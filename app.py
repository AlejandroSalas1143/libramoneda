from __future__ import annotations

# =======================
# FastAPI + Simulador + Google Sheets (OAuth2 token.json)
# =======================
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, PositiveInt, EmailStr, validator
from decimal import Decimal, getcontext, ROUND_HALF_UP
from datetime import date, datetime, timedelta
import calendar
from typing import List, Optional
import os
import json
from pathlib import Path
import uuid
import logging

# Google APIs
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# =======================
# Configuración general
# =======================
PRODUCT_CONFIG = {
    "base_rate_monthly": Decimal("0.0184"),   # 1,84% mensual (sin aval)
    "total_rate_monthly": Decimal("0.0705"),  # 7,05% mensual (con aval)
    "iva_rate": Decimal("0.19"),              # 19%
    "round_to": Decimal("1")                  # redondeo a pesos
}

SHEET_TITLE = "Solicitudes de Crédito - Libramoneda"
SHEET_TAB = "Solicitudes"
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("libramoneda_api")

# Precisión y redondeo financiero
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP
R = PRODUCT_CONFIG["round_to"]

app = FastAPI(title="Credit Simulator API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # en producción, restrínge a tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =======================
# Utilidades de fechas
# =======================
def month_end(d: date) -> date:
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last_day)

def next_month_end(d: date) -> date:
    y, m = d.year, d.month
    if m == 12:
        y, m = y + 1, 1
    else:
        m += 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, last)

def diff_days_exclusive(start: date, end: date) -> int:
    return (end - start).days

# =======================
# PMT (sistema francés)
# =======================
def pmt(principal: Decimal, i: Decimal, n: int) -> Decimal:
    if i == 0:
        return (principal / Decimal(n)).quantize(R)
    num = principal * i
    den = Decimal(1) - (Decimal(1) + i) ** Decimal(-n)
    return (num / den).quantize(R)

# =======================
# Modelos - Simulador
# =======================
class SimulateRequest(BaseModel):
    amount: Decimal = Field(..., gt=Decimal("0"))
    periods: PositiveInt
    start_date: date | None = None

class ScheduleRow(BaseModel):
    cuota_no: int
    fecha_inicial: date
    fin_mes_primera_cuota: date | None
    fecha_final: date
    mes_anio: str
    tasa_usura: Decimal
    tasa_interes: Decimal
    dias: int
    saldo_inicial: Decimal
    valor_cuota_mensual: Decimal
    abono_capital: Decimal
    aval: Decimal
    iva_aval: Decimal
    interes_primera_cuota: Decimal
    intereses: Decimal
    saldo_final: Decimal

class SimulateResponse(BaseModel):
    amount: Decimal
    periods: int
    payment_monthly: Decimal
    notes: str
    schedule: List[ScheduleRow]

# =======================
# Modelos - Solicitud de crédito
# =======================
class DatosPersonales(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=120)
    apellido: str = Field(..., min_length=1, max_length=120)
    cedula: str = Field(..., min_length=7, max_length=15)
    telefono: str = Field(..., min_length=10, max_length=15)
    email: EmailStr
    fecha_nacimiento: Optional[date] = None
    ciudad: Optional[str] = None
    direccion: Optional[str] = None

    @validator('cedula')
    def validar_cedula(cls, v):
        v = v.replace(' ', '').replace('-', '')
        if not v.isdigit():
            raise ValueError('La cédula debe contener solo números')
        return v

    @validator('telefono')
    def validar_telefono(cls, v):
        v = v.replace(' ', '').replace('-', '').replace('(', '').replace(')', '').replace('+', '')
        if not v.isdigit():
            raise ValueError('El teléfono debe contener solo números')
        return v

class InformacionFinanciera(BaseModel):
    ingresos_mensuales: Decimal = Field(..., gt=Decimal("0"))
    gastos_mensuales: Decimal = Field(..., ge=Decimal("0"))
    empresa: Optional[str] = None
    antiguedad_laboral_meses: Optional[int] = Field(default=0, ge=0)

class DetallesCredito(BaseModel):
    monto_solicitado: Decimal = Field(..., gt=Decimal("0"))
    plazo_meses: PositiveInt = Field(..., le=60)
    cuota_mensual_calculada: Optional[Decimal] = None

class SolicitudCredito(BaseModel):
    datos_personales: DatosPersonales
    informacion_financiera: InformacionFinanciera
    detalles_credito: DetallesCredito

    acepta_tratamiento_datos: bool
    acepta_terminos_condiciones: bool
    acepta_consulta_centrales_riesgo: bool

    fecha_solicitud: Optional[datetime] = None

    @validator('acepta_tratamiento_datos', 'acepta_terminos_condiciones', 'acepta_consulta_centrales_riesgo')
    def validar_consentimientos(cls, v):
        if not v:
            raise ValueError('Debe aceptar todos los consentimientos requeridos')
        return v

class SolicitudResponse(BaseModel):
    id_solicitud: str
    mensaje: str
    cuota_estimada: Decimal
    siguiente_paso: str
    whatsapp_url: Optional[str] = None

# =======================
# Utils de negocio
# =======================
def calcular_cuota_mensual(monto: Decimal, plazo: int) -> Decimal:
    i0 = PRODUCT_CONFIG["base_rate_monthly"]
    i1 = PRODUCT_CONFIG["total_rate_monthly"]
    iva = PRODUCT_CONFIG["iva_rate"]
    pay_base = pmt(monto, i0, plazo)
    pay_with_aval = pmt(monto, i1, plazo)
    aval_monthly = (pay_with_aval - pay_base).quantize(R)
    iva_aval = (aval_monthly * iva).quantize(R)
    return (pay_base + aval_monthly + iva_aval).quantize(R)

def generar_whatsapp_url(telefono: str, id_solicitud: str) -> str:
    n = telefono.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    if not n.startswith('57'):
        n = '57' + n
    msg = "Hola! Quiero continuar con mi solicitud de crédito. Mi ID es: " + id_solicitud
    return f"https://wa.me/{n}?text={msg.replace(' ', '%20')}"

# =======================
# Google Sheets (OAuth2 con token persistente)
# =======================
def _load_oauth_credentials() -> Credentials:
    token_env = os.getenv("GOOGLE_OAUTH_TOKEN_JSON")
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPE)
    elif Path("token.json").exists():
        creds = Credentials.from_authorized_user_file("token.json", SCOPE)
    else:
        raise RuntimeError(
            "No se encontró token OAuth. Genera token.json con un flujo local o define GOOGLE_OAUTH_TOKEN_JSON."
        )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            if Path("token.json").exists():
                with open("token.json", "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
        else:
            raise RuntimeError("Credenciales inválidas y sin refresh_token. Reautoriza el acceso.")
    return creds

def conectar_google_sheets():
    creds = _load_oauth_credentials()
    return gspread.authorize(creds)

def obtener_o_crear_spreadsheet(client) -> gspread.Spreadsheet:
    try:
        return client.open(SHEET_TITLE)
    except gspread.SpreadsheetNotFound:
        ss = client.create(SHEET_TITLE)
        logger.info(f"Spreadsheet creada: {ss.url}")
        return ss

def inicializar_worksheet(spreadsheet: gspread.Spreadsheet):
    try:
        ws = spreadsheet.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_TAB, rows=2000, cols=50)
        headers = [
            'ID_Solicitud', 'Fecha_Solicitud',
            'Nombre', 'Apellido', 'Cedula', 'Telefono', 'Email', 'Fecha_Nacimiento', 'Ciudad', 'Direccion',
            'Ingresos_Mensuales', 'Gastos_Mensuales', 'Empresa', 'Antiguedad_Laboral_Meses',
            'Monto_Solicitado', 'Plazo_Meses', 'Cuota_Estimada',
            'Acepta_Tratamiento_Datos', 'Acepta_Terminos', 'Acepta_Consulta_Centrales', 'Estado'
        ]
        ws.update("A1", [headers])
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
    return ws

def _normalize_bool(v: bool) -> str:
    return "TRUE" if bool(v) else "FALSE"

def guardar_en_sheets(solicitud: SolicitudCredito, id_solicitud: str, cuota_estimada: Decimal) -> bool:
    try:
        client = conectar_google_sheets()
        ss = obtener_o_crear_spreadsheet(client)
        ws = inicializar_worksheet(ss)

        fila = [
            id_solicitud,
            (solicitud.fecha_solicitud or datetime.now()).strftime('%Y-%m-%d %H:%M:%S'),

            solicitud.datos_personales.nombre,
            solicitud.datos_personales.apellido,
            solicitud.datos_personales.cedula,
            solicitud.datos_personales.telefono,
            str(solicitud.datos_personales.email),
            (solicitud.datos_personales.fecha_nacimiento.strftime('%Y-%m-%d')
             if solicitud.datos_personales.fecha_nacimiento else ''),
            (solicitud.datos_personales.ciudad or ''),
            (solicitud.datos_personales.direccion or ''),

            float(solicitud.informacion_financiera.ingresos_mensuales),
            float(solicitud.informacion_financiera.gastos_mensuales),
            (solicitud.informacion_financiera.empresa or ''),
            (solicitud.informacion_financiera.antiguedad_laboral_meses or 0),

            float(solicitud.detalles_credito.monto_solicitado),
            solicitud.detalles_credito.plazo_meses,
            float(cuota_estimada),

            _normalize_bool(solicitud.acepta_tratamiento_datos),
            _normalize_bool(solicitud.acepta_terminos_condiciones),
            _normalize_bool(solicitud.acepta_consulta_centrales_riesgo),

            'NUEVO',
        ]

        ws.append_row(fila, value_input_option="USER_ENTERED")
        logger.info(f"Solicitud {id_solicitud} guardada en Google Sheets")
        return True
    except Exception as e:
        logger.error(f"Error guardando en Google Sheets: {e}")
        return False

# =======================
# Endpoints
# =======================
@app.post("/simulate", response_model=SimulateResponse)
def simulate(req: SimulateRequest):
    P = req.amount.quantize(R)
    n = req.periods
    i0 = PRODUCT_CONFIG["base_rate_monthly"]
    i1 = PRODUCT_CONFIG["total_rate_monthly"]
    iva = PRODUCT_CONFIG["iva_rate"]
    start = req.start_date or date.today()

    pay_base = pmt(P, i0, n)
    pay_with_aval = pmt(P, i1, n)
    aval_monthly = (pay_with_aval - pay_base).quantize(R)
    iva_aval = (aval_monthly * iva).quantize(R)
    payment_monthly = (pay_base + aval_monthly + iva_aval).quantize(R)

    schedule: List[ScheduleRow] = []
    saldo = P

    # 1ª cuota (interés del cierre del mes del desembolso)
    fin_mes_desembolso = month_end(start)
    fecha_final_1 = next_month_end(start)
    dias_0 = diff_days_exclusive(start, fin_mes_desembolso)
    dias_total_1 = diff_days_exclusive(start, fecha_final_1)
    interes_primera = (saldo * i0 * Decimal(dias_0) / Decimal(30)).quantize(R)

    # Interés de un mes “contractual”
    interes_30 = (saldo * i0 * Decimal(30) / Decimal(30)).quantize(R)
    interes_tramo_restante = (interes_30 - interes_primera).quantize(R)

    payment_first_month = payment_monthly - interes_tramo_restante
    abono_cap_1 = (payment_first_month - aval_monthly - iva_aval - interes_primera).quantize(R)
    if abono_cap_1 < 0:
        abono_cap_1 = Decimal("0")
    if abono_cap_1 > saldo:
        abono_cap_1 = saldo
    saldo = (saldo - abono_cap_1).quantize(R)

    valor_cuota_1 = (abono_cap_1 + aval_monthly + iva_aval + interes_primera).quantize(R)

    schedule.append(ScheduleRow(
        cuota_no=1,
        fecha_inicial=start,
        fin_mes_primera_cuota=fin_mes_desembolso,
        fecha_final=fecha_final_1,
        mes_anio=f"{fecha_final_1.month}-{fecha_final_1.year}",
        tasa_usura=Decimal("0.2501"),
        tasa_interes=i0.quantize(Decimal("0.0001")),
        dias=dias_total_1,
        saldo_inicial=P,
        valor_cuota_mensual=valor_cuota_1,
        abono_capital=abono_cap_1,
        aval=aval_monthly,
        iva_aval=iva_aval,
        interes_primera_cuota=interes_primera,
        intereses=Decimal("0"),
        saldo_final=saldo
    ))

    # Cuotas 2..n
    prev_final = fecha_final_1
    for k in range(2, n + 1):
        fecha_inicial_k = prev_final + timedelta(days=1)
        fecha_final_k = month_end(fecha_inicial_k)
        dias_k = diff_days_exclusive(fecha_inicial_k, fecha_final_k)

        interes_k = (saldo * i0 * Decimal(dias_k) / Decimal(30)).quantize(R)
        # Cuota del mes variable (composición real)
        abono_cap_k = (payment_monthly - aval_monthly - iva_aval - interes_k).quantize(R)
        if abono_cap_k < 0:
            abono_cap_k = Decimal("0")
        if k == n:
            abono_cap_k = saldo

        saldo = (saldo - abono_cap_k).quantize(R)
        valor_cuota_k = (abono_cap_k + interes_k + aval_monthly + iva_aval).quantize(R)

        schedule.append(ScheduleRow(
            cuota_no=k,
            fecha_inicial=fecha_inicial_k,
            fin_mes_primera_cuota=None,
            fecha_final=fecha_final_k,
            mes_anio=f"{fecha_final_k.month}-{fecha_final_k.year}",
            tasa_usura=Decimal("0.2501"),
            tasa_interes=i0.quantize(Decimal("0.0001")),
            dias=dias_k,
            saldo_inicial=schedule[-1].saldo_final,
            valor_cuota_mensual=valor_cuota_k,
            abono_capital=abono_cap_k,
            aval=aval_monthly,
            iva_aval=iva_aval,
            interes_primera_cuota=Decimal("0"),
            intereses=interes_k,
            saldo_final=saldo
        ))
        prev_final = fecha_final_k

    return SimulateResponse(
        amount=P,
        periods=n,
        payment_monthly=payment_monthly,
        notes=("Valores en COP. 'Valor Cuota Mensual' es lo efectivamente cobrado: "
               "abono + intereses + aval + IVA. La primera fila incluye el interés de cierre del mes del desembolso. "
               "'payment_monthly' es la cuota contractual (con aval+IVA prorrateado)."),
        schedule=schedule
    )

@app.post("/solicitar-credito", response_model=SolicitudResponse)
def solicitar_credito(solicitud: SolicitudCredito):
    try:
        id_solicitud = str(uuid.uuid4())[:8].upper()
        if not solicitud.fecha_solicitud:
            solicitud.fecha_solicitud = datetime.now()

        cuota_estimada = calcular_cuota_mensual(
            solicitud.detalles_credito.monto_solicitado,
            solicitud.detalles_credito.plazo_meses
        )
        solicitud.detalles_credito.cuota_mensual_calculada = cuota_estimada

        ok = guardar_en_sheets(solicitud, id_solicitud, cuota_estimada)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error al guardar la solicitud en Google Sheets"
            )

        whatsapp_url = generar_whatsapp_url(
            solicitud.datos_personales.telefono,
            id_solicitud
        )

        return SolicitudResponse(
            id_solicitud=id_solicitud,
            mensaje=f"Solicitud recibida exitosamente. Tu cuota estimada es ${cuota_estimada:,.0f} COP",
            cuota_estimada=cuota_estimada,
            siguiente_paso="Continúa el proceso por WhatsApp para enviar tus documentos",
            whatsapp_url=whatsapp_url
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error procesando solicitud")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error procesando la solicitud: {str(e)}"
        )

@app.get("/solicitudes")
def listar_solicitudes():
    try:
        client = conectar_google_sheets()
        spreadsheet = obtener_o_crear_spreadsheet(client)
        worksheet = inicializar_worksheet(spreadsheet)
        datos = worksheet.get_all_records()
        return {"solicitudes": datos, "total": len(datos), "google_sheets_url": spreadsheet.url}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error consultando solicitudes: {str(e)}"
        )

@app.get("/solicitud/{id_solicitud}")
def consultar_solicitud(id_solicitud: str):
    try:
        client = conectar_google_sheets()
        spreadsheet = obtener_o_crear_spreadsheet(client)
        worksheet = inicializar_worksheet(spreadsheet)
        try:
            cell = worksheet.find(id_solicitud.upper())
            if cell:
                headers = worksheet.row_values(1)
                row_data = worksheet.row_values(cell.row)
                return dict(zip(headers, row_data))
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        except gspread.exceptions.CellNotFound:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error consultando solicitud: {str(e)}"
        )

@app.get("/health")
def health_check():
    return {"status": "OK", "version": "2.0.0"}

# Para ejecutar local:
# uvicorn nombre_archivo:app --reload
