from __future__ import annotations

# =======================
# FastAPI + Simulador + Google Sheets + Google Drive
# =======================
import io
import os
import json
import uuid
import re
import calendar
import logging
from pathlib import Path
from decimal import Decimal, getcontext, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, PositiveInt, EmailStr, field_validator

# Google APIs
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# =======================
# Configuración general
# =======================
PRODUCT_CONFIG = {
    "base_rate_monthly": Decimal("0.0183"),   # 1,84% mensual (sin aval)
    "rate_aval_payroll": Decimal("0.0705"),  # 7,05% mensual (con aval) para creditos de libranza
    "rate_aval_individualorcompany": Decimal("0.0405"), # 4,05% mensual (con aval) para creditos de personas naturales o juridicas
    "iva_rate": Decimal("0.19"),              # 19%
    "round_to": Decimal("1")                  # redondeo a pesos
}

SHEET_TITLE = "Solicitudes de Crédito - Libramoneda"
SHEET_TAB = "Solicitudes"
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Google Drive
DRIVE_ROOT_FOLDER_NAME = "Libramoneda - Solicitudes"
DRIVE_PUBLIC_LINKS = os.getenv("DRIVE_PUBLIC_LINKS", "true").lower() == "true"
# Si pones DRIVE_PUBLIC_LINKS=true, los links serán accesibles "con el enlace"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("libramoneda_api")

# Precisión y redondeo financiero
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP
R = PRODUCT_CONFIG["round_to"]

app = FastAPI(title="Credit Simulator API", version="2.2.0")
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
    tipo_credito: Optional[str] = None   # e.g., "libranza" | "particular" | "consumo"
    tipo_persona: Optional[str] = None   # e.g., "natural" | "juridica"

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

    @field_validator('cedula')
    @classmethod
    def validar_cedula(cls, v):
        v = v.replace(' ', '').replace('-', '')
        if not v.isdigit():
            raise ValueError('La cédula debe contener solo números')
        return v

    @field_validator('telefono')
    @classmethod
    def validar_telefono(cls, v):
        v = v.replace(' ', '').replace('-', '').replace('(', '').replace(')', '').replace('+', '')
        if not v.isdigit():
            raise ValueError('El teléfono debe contener solo números')
        return v


class InformacionHogar(BaseModel):
    vivienda_propia: bool
    num_personas_aportan: int = Field(..., ge=0)
    num_personas_cargo: int = Field(..., ge=0)


class InformacionFinanciera(BaseModel):
    # NUEVOS campos soportados
    ingresos_mensuales_actividad: Optional[Decimal] = Field(default=Decimal("0"))
    otros_ingresos: Optional[Decimal] = Field(default=Decimal("0"))
    gastos_personales: Optional[Decimal] = Field(default=Decimal("0"))
    gastos_financieros: Optional[Decimal] = Field(default=Decimal("0"))

    # Ya existentes (mantener compatibilidad)
    ingresos_mensuales: Decimal = Field(..., gt=Decimal("0"))
    gastos_mensuales: Decimal = Field(..., ge=Decimal("0"))
    empresa: Optional[str] = None
    antiguedad_laboral_meses: Optional[int] = Field(default=0, ge=0)


class DetallesCredito(BaseModel):
    monto_solicitado: Decimal = Field(..., gt=Decimal("0"))
    plazo_meses: PositiveInt = Field(..., le=60)

    # del front (opcionales): cuota_estimada y tasa
    cuota_estimada: Optional[Decimal] = None
    tasa: Optional[Decimal] = None
    tipo_credito: Optional[str] = None   # "libranza" o lo que uses en front
    tipo_persona: Optional[str] = None   # "natural" | "juridica"


class SolicitudCredito(BaseModel):
    datos_personales: DatosPersonales
    informacion_hogar: InformacionHogar
    informacion_financiera: InformacionFinanciera
    detalles_credito: DetallesCredito

    acepta_tratamiento_datos: bool
    acepta_terminos_condiciones: bool
    acepta_consulta_centrales_riesgo: bool

    fecha_solicitud: Optional[datetime] = None

    @field_validator('acepta_tratamiento_datos', 'acepta_terminos_condiciones', 'acepta_consulta_centrales_riesgo')
    @classmethod
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
def calcular_cuota_mensual(monto: Decimal, plazo: int, tipo_credito: Optional[str] = None, tipo_persona: Optional[str] = None) -> Decimal:
    i0 = PRODUCT_CONFIG["base_rate_monthly"]
    i1 = seleccionar_tasa_aval(monto, tipo_credito, tipo_persona)
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

def seleccionar_tasa_aval(monto: Decimal, tipo_credito: Optional[str], tipo_persona: Optional[str]) -> Decimal:
    """
    Regla:
      - Si es libranza => siempre rate_aval_payroll (7,05%), sin importar monto.
      - Si NO es libranza:
          - monto > 5'000.000 => rate_aval_individualorcompany (4,05%)
          - monto <= 5'000.000 => rate_aval_payroll (7,05%)
    """
    if (tipo_credito or "").strip().lower() == "libranza":
        return PRODUCT_CONFIG["rate_aval_payroll"]
    return (
        PRODUCT_CONFIG["rate_aval_individualorcompany"]
        if monto > Decimal("5000000")
        else PRODUCT_CONFIG["rate_aval_payroll"]
    )

# =======================
# Google Auth base
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

# =======================
# Google Sheets helpers
# =======================
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
    FULL_HEADERS = [
        # Identificación y fecha
        'ID_Solicitud', 'Fecha_Solicitud',

        # Datos personales
        'Nombre', 'Apellido', 'Cedula', 'Telefono', 'Email',
        'Fecha_Nacimiento', 'Ciudad', 'Direccion',

        # Información hogar (NUEVO)
        'Vivienda_Propia', 'Num_Personas_Aportan', 'Num_Personas_Cargo',

        # Información financiera (ampliado)
        'Ingresos_Mensuales_Actividad', 'Otros_Ingresos',
        'Gastos_Personales', 'Gastos_Financieros',
        'Ingresos_Mensuales', 'Gastos_Mensuales',
        'Empresa', 'Antiguedad_Laboral_Meses',

        # Detalles de crédito
        'Monto_Solicitado', 'Plazo_Meses',
        'Cuota_Estimada_Front',         # lo que mandó el front (opcional)
        'Tasa_Front',                   # la tasa que mandó el front (opcional)

        # Consentimientos y estado
        'Acepta_Tratamiento_Datos', 'Acepta_Terminos',
        'Acepta_Consulta_Centrales', 'Estado',

        # Enlaces
        'Carpeta_Drive', 'URL_Doc_Cedula',
        'URL_Comprobante_Ingresos', 'URL_FormatoSolicitud'
    ]

    try:
        ws = spreadsheet.worksheet(SHEET_TAB)
        # Migración de encabezado si cambia
        current_headers = ws.row_values(1)
        if current_headers != FULL_HEADERS:
            ws.update("A1", [FULL_HEADERS])
            try:
                ws.freeze(rows=1)
            except Exception:
                pass
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_TAB, rows=2000, cols=80)
        ws.update("A1", [FULL_HEADERS])
        try:
            ws.freeze(rows=1)
        except Exception:
            pass

    return ws

def _normalize_bool(v: bool) -> str:
    return "TRUE" if bool(v) else "FALSE"

def guardar_en_sheets(
    solicitud: SolicitudCredito,
    id_solicitud: str,
    cuota_estimada_backend: Decimal,
    carpeta_drive_url: str,
    url_doc_cedula: str,
    url_comp_ing: str,
    url_FormatoSolicitud: str,
) -> bool:
    try:
        client = conectar_google_sheets()
        ss = obtener_o_crear_spreadsheet(client)
        ws = inicializar_worksheet(ss)

        def link_formula(url: str, label: str) -> str:
            return f'=HYPERLINK("{url}", "{label}")' if url else ""

        dp = solicitud.datos_personales
        ih = solicitud.informacion_hogar
        inf = solicitud.informacion_financiera
        dc = solicitud.detalles_credito

        fila = [
            # Identificación y fecha
            id_solicitud,
            (solicitud.fecha_solicitud or datetime.now()).strftime('%Y-%m-%d %H:%M:%S'),

            # Datos personales
            dp.nombre, dp.apellido, dp.cedula, dp.telefono, str(dp.email),
            (dp.fecha_nacimiento.strftime('%Y-%m-%d') if dp.fecha_nacimiento else ''),
            (dp.ciudad or ''), (dp.direccion or ''),

            # Información hogar
            _normalize_bool(ih.vivienda_propia),
            ih.num_personas_aportan,
            ih.num_personas_cargo,

            # Información financiera ampliada
            float(inf.ingresos_mensuales_actividad or 0),
            float(inf.otros_ingresos or 0),
            float(inf.gastos_personales or 0),
            float(inf.gastos_financieros or 0),

            float(inf.ingresos_mensuales),
            float(inf.gastos_mensuales),
            (inf.empresa or ''),
            (inf.antiguedad_laboral_meses or 0),

            # Detalles de crédito
            float(dc.monto_solicitado),
            dc.plazo_meses,
            (float(dc.cuota_estimada) if dc.cuota_estimada is not None else ''),
            (float(dc.tasa) if dc.tasa is not None else ''),

            # Consentimientos y estado
            _normalize_bool(solicitud.acepta_tratamiento_datos),
            _normalize_bool(solicitud.acepta_terminos_condiciones),
            _normalize_bool(solicitud.acepta_consulta_centrales_riesgo),
            'NUEVO',

            # Enlaces
            link_formula(carpeta_drive_url, "Carpeta Drive"),
            link_formula(url_doc_cedula, "Cédula"),
            link_formula(url_comp_ing, "Comprobante Ingresos"),
            link_formula(url_FormatoSolicitud, "FormatoSolicitud"),
        ]

        ws.append_row(fila, value_input_option="USER_ENTERED")
        logger.info(f"Solicitud {id_solicitud} guardada en Google Sheets")
        return True
    except Exception as e:
        logger.error(f"Error guardando en Google Sheets: {e}")
        return False

# =======================
# Google Drive helpers
# =======================
def get_drive_service():
    creds = _load_oauth_credentials()
    return build("drive", "v3", credentials=creds)

def _find_or_create_folder(service, name: str, parent_id: Optional[str] = None) -> str:
    q = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = service.files().list(q=q, fields="files(id, name)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def _sanitize_for_path(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\-.]+", "", s, flags=re.UNICODE)
    return s[:80] if len(s) > 80 else s

def _ensure_root_and_case_folder(service, id_solicitud: str, cedula: str, nombre: str, apellido: str) -> tuple[str, str]:
    root_id = _find_or_create_folder(service, DRIVE_ROOT_FOLDER_NAME)
    folder_name = f"SOL-{id_solicitud} - {cedula} - {_sanitize_for_path(nombre)}_{_sanitize_for_path(apellido)}"
    case_id = _find_or_create_folder(service, folder_name, parent_id=root_id)
    return root_id, case_id

def _maybe_make_public(service, file_id: str):
    if not DRIVE_PUBLIC_LINKS:
        return
    try:
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            fields="id"
        ).execute()
    except Exception:
        pass

def upload_to_drive(service, folder_id: str, upload: UploadFile, suggested_name: Optional[str] = None) -> dict:
    data = upload.file.read()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=(upload.content_type or "application/octet-stream"), resumable=False)
    fname = suggested_name or upload.filename
    metadata = {"name": fname, "parents": [folder_id]}
    file = service.files().create(body=metadata, media_body=media, fields="id, webViewLink, name").execute()
    _maybe_make_public(service, file["id"])
    file = service.files().get(fileId=file["id"], fields="id, webViewLink, name").execute()
    return file

def folder_web_link(service, folder_id: str) -> str:
    _maybe_make_public(service, folder_id)
    f = service.files().get(fileId=folder_id, fields="id, webViewLink").execute()
    return f.get("webViewLink")

# =======================
# Endpoints
# =======================
@app.post("/simulate", response_model=SimulateResponse)
def simulate(req: SimulateRequest):
    P = req.amount.quantize(R)
    n = req.periods
    i0 = PRODUCT_CONFIG["base_rate_monthly"]
    i1 = seleccionar_tasa_aval(P, req.tipo_credito, req.tipo_persona)   # ← NUEVO
    iva = PRODUCT_CONFIG["iva_rate"]
    start = req.start_date or date.today()

    pay_base = pmt(P, i0, n)
    pay_with_aval = pmt(P, i1, n)
    aval_monthly = (pay_with_aval - pay_base).quantize(R)
    iva_aval = (aval_monthly * iva).quantize(R)
    payment_monthly = (pay_base + aval_monthly + iva_aval).quantize(R)

    schedule: List[ScheduleRow] = []
    saldo = P

    # 1ª cuota
    fin_mes_desembolso = month_end(start)
    fecha_final_1 = next_month_end(start)
    dias_0 = diff_days_exclusive(start, fin_mes_desembolso)
    dias_total_1 = diff_days_exclusive(start, fecha_final_1)
    interes_primera = (saldo * i0 * Decimal(dias_0) / Decimal(30)).quantize(R)

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

# === Endpoint MULTIPART: carpeta por solicitud con 3 archivos requeridos ===
@app.post("/solicitar-credito", response_model=SolicitudResponse)
async def solicitar_credito(
    payload: Optional[str] = Form(None),
    payload_file: Optional[UploadFile] = File(None),  # ← por si llega como Blob
    doc_cedula: UploadFile = File(...),
    doc_comprobante_ingresos: UploadFile = File(...),
    doc_FormatoSolicitud: UploadFile = File(...),
):
    try:
        # Normaliza payload
        if payload is None and payload_file is not None:
            payload = (await payload_file.read()).decode("utf-8")

        if not payload:
            raise HTTPException(status_code=422, detail="Falta 'payload'")

        data = json.loads(payload)
        solicitud = SolicitudCredito(**data)

        id_solicitud = str(uuid.uuid4())[:8].upper()
        if not solicitud.fecha_solicitud:
            solicitud.fecha_solicitud = datetime.now()

        # 2) Calcular cuota estimada
        cuota_estimada = calcular_cuota_mensual(
            solicitud.detalles_credito.monto_solicitado,
            solicitud.detalles_credito.plazo_meses
        )
        # Guarda el cálculo DENTRO del objeto, no lo reemplaces
        solicitud.detalles_credito.cuota_estimada = cuota_estimada

        # 3) Crear/obtener carpeta de la solicitud en Google Drive
        drive = get_drive_service()
        dp = solicitud.datos_personales
        _, case_folder_id = _ensure_root_and_case_folder(
            drive, id_solicitud, dp.cedula, dp.nombre, dp.apellido
        )
        carpeta_link = folder_web_link(drive, case_folder_id)

        base_name = f"{dp.cedula}_{dp.nombre}_{dp.apellido}".replace(" ", "_")

        # 4) Subir exactamente 3 archivos dentro de esa carpeta
        cedula_file = upload_to_drive(
            drive, case_folder_id, doc_cedula, suggested_name=f"{base_name}_cedula"
        )
        url_cedula = cedula_file["webViewLink"]

        comp_file = upload_to_drive(
            drive, case_folder_id, doc_comprobante_ingresos, suggested_name=f"{base_name}_comprobante_ingresos"
        )
        url_comp_ing = comp_file["webViewLink"]

        ext_file = upload_to_drive(
            drive, case_folder_id, doc_FormatoSolicitud, suggested_name=f"{base_name}_FormatoSolicitud"
        )
        url_FormatoSolicitud = ext_file["webViewLink"]

        # 5) Guardar la fila en Sheets, con HYPERLINKs
        ok = guardar_en_sheets(
            solicitud, id_solicitud, cuota_estimada,
            carpeta_drive_url=carpeta_link,
            url_doc_cedula=url_cedula,
            url_comp_ing=url_comp_ing,
            url_FormatoSolicitud=url_FormatoSolicitud,
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error al guardar la solicitud en Google Sheets"
            )

        # 6) WhatsApp de continuación
        whatsapp_url = generar_whatsapp_url(solicitud.datos_personales.telefono, id_solicitud)

        return SolicitudResponse(
            id_solicitud=id_solicitud,
            mensaje=f"Solicitud recibida exitosamente. Tu cuota estimada es ${cuota_estimada:,.0f} COP",
            cuota_estimada=cuota_estimada,
            siguiente_paso="Hemos cargado tus documentos en la carpeta de la solicitud.",
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

# === Endpoints de consulta (Sheets) ===
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
    return {"status": "OK", "version": "2.2.0"}

# Para ejecutar local:
# uvicorn nombre_archivo:app --reload
