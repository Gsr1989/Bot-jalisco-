from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz  # PyMuPDF
import pytz
import pdf417gen
from PIL import Image
import random

# Importaciones adicionales
from io import BytesIO
import base64
from pdf417gen import encode, render_image
import qrcode
import string
import csv
import json
import io
import time
import re  # para filtrar folios no numéricos

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "jalisco1.pdf"  # PDF principal (el completo)
PLANTILLA_BUENO = "jalisco.pdf"  # PDF simple (solo fecha y serie)

# Precio del permiso
PRECIO_PERMISO = 250

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

# URL de consulta para QRs
URL_CONSULTA_BASE = "https://serviciodigital-jaliscogobmx.onrender.com"

# Coordenadas para el QR (en una posición que no interfiera con el contenido existente)
coords_qr_dinamico = {
    "x": 940,
    "y": 630,
    "ancho": 155,
    "alto": 155
}

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ============ FOLIOS CONSECUTIVOS: INICIO 900345876, +1, SINCRONIZADO ============
FOLIO_INICIO = 900_345_876          # 9 dígitos, primer folio
FOLIO_FIN    = 999_999_999          # 9 dígitos, límite duro
_folio_cursor = FOLIO_INICIO - 1
_folio_lock = asyncio.Lock()

def _leer_cursor_local():
    """Lee un cursor local (opcional) para continuidad si no hay DB."""
    try:
        with open("folio_cursor_local.txt") as f:
            return int(f.read().strip())
    except Exception:
        return None

def _guardar_cursor_local(valor: int):
    try:
        with open("folio_cursor_local.txt", "w") as f:
            f.write(str(valor))
    except Exception as e:
        print(f"[WARN] No se pudo persistir cursor local: {e}")

def _leer_ultimo_folio_en_db() -> int | None:
    """
    Devuelve el MAYOR folio NUMÉRICO encontrado en DB >= FOLIO_INICIO.
    Ignora folios alfanuméricos (p. ej. 'SR2042').
    """
    try:
        resp = (
            supabase.table("folios_registrados")
            .select("folio")
            .order("folio", desc=True)
            .limit(1000)
            .execute()
        )
        max_num: int | None = None
        for row in (resp.data or []):
            s = str(row.get("folio", "")).strip()
            if re.fullmatch(r"\d+", s):
                val = int(s)
                if val >= FOLIO_INICIO and (max_num is None or val > max_num):
                    max_num = val

        if max_num is not None:
            print(f"[FOLIO][DB] Último folio numérico en DB: {max_num}")
            return max_num

        print("[FOLIO][DB] No se hallaron folios numéricos válidos.")
        return None

    except Exception as e:
        print(f"[ERROR] Consultando último folio en DB (robusto): {e}")
        return None

async def inicializar_folio_cursor():
    """
    Define _folio_cursor al arrancar:
    - Toma el mayor folio de Supabase (si existe y es numérico)
    - Si no, intenta cursor local
    - Si no, arranca en FOLIO_INICIO - 1
    """
    global _folio_cursor
    ultimo_db = _leer_ultimo_folio_en_db()
    if ultimo_db is not None:
        _folio_cursor = ultimo_db
        _guardar_cursor_local(_folio_cursor)
        print(f"[FOLIO] Cursor inicializado desde DB: {_folio_cursor}")
        return

    ultimo_local = _leer_cursor_local()
    if ultimo_local is not None and ultimo_local >= (FOLIO_INICIO - 1):
        _folio_cursor = ultimo_local
        print(f"[FOLIO] Cursor inicializado desde archivo local: {_folio_cursor}")
    else:
        _folio_cursor = FOLIO_INICIO - 1
        print(f"[FOLIO] Cursor inicializado al valor base: {_folio_cursor}")

async def generar_folio_consecutivo() -> str:
    """
    Genera el siguiente folio consecutivo (+1) de forma segura (con lock).
    Persiste un cursor local para continuidad si se reinicia.
    """
    global _folio_cursor
    async with _folio_lock:
        _folio_cursor += 1
        if _folio_cursor < FOLIO_INICIO:
            _folio_cursor = FOLIO_INICIO
        _guardar_cursor_local(_folio_cursor)
        folio = str(_folio_cursor)
        print(f"[FOLIO] Generado: {folio}")
        return folio

async def guardar_folio_con_reintento(datos, user_id, username):
    """
    Inserta el folio en DB con reintentos ante colisiones (UNIQUE).
    Si hay duplicado, genera el siguiente (+1) y vuelve a intentar.
    """
    max_intentos = 8
    for intento in range(1, max_intentos + 1):
        # Si el folio aún no existe en datos, genera uno; si ya existe por flujo previo, úsalo.
        if "folio" not in datos or not datos["folio"]:
            datos["folio"] = await generar_folio_consecutivo()

        try:
            supabase.table("folios_registrados").insert({
                "folio": datos["folio"],               # se guarda como texto o número, DB decide
                "marca": datos["marca"],
                "linea": datos["linea"],
                "anio": datos["anio"],
                "numero_serie": datos["serie"],
                "numero_motor": datos["motor"],
                "color": datos["color"],
                "nombre": datos["nombre"],
                "fecha_expedicion": datos["fecha_exp"].date().isoformat(),
                "fecha_vencimiento": datos["fecha_ven"].date().isoformat(),
                "entidad": "Jalisco",
                "estado": "PENDIENTE",
                "user_id": user_id,
                "username": username or "Sin username"
            }).execute()
            print(f"[ÉXITO] ✅ Folio {datos['folio']} guardado en DB")
            # Actualizar cursor local a este último definitivamente usado
            try:
                valor_int = int(datos["folio"])
                _guardar_cursor_local(valor_int)
            except Exception:
                pass
            return True

        except Exception as e:
            em = str(e).lower()
            if "duplicate" in em or "unique constraint" in em or "23505" in em:
                print(f"[DUPLICADO] Folio {datos['folio']} ya existe, intento {intento}/{max_intentos}")
                # Forzar siguiente folio y reintentar
                datos["folio"] = None
                continue
            print(f"[ERROR BD] {e}")
            return False

    print("[ERROR FATAL] No se pudo guardar tras múltiples intentos")
    return False

def generar_folio_jalisco_sync() -> str:
    """
    Versión sincrónica minimal solo para logs puntuales donde se requiera un ID previo;
    el folio real para DB debe venir de guardar_folio_con_reintento().
    """
    valor = max(_folio_cursor + 1, FOLIO_INICIO)
    return str(valor)

# ------------ TIMER MANAGEMENT - AUTOELIMINACIÓN A LAS 12 HORAS ------------
# Avisos en los últimos 60, 30 y 10 minutos. Notifica al eliminar.
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [lista_de_folios_activos]}
pending_comprobantes = {}  # {user_id: folio} para usuarios esperando especificar folio

TOTAL_MINUTOS_TIMER = 12 * 60  # 720

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después de 12 horas y avisa."""
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario si está disponible
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - ESTADO DE JALISCO\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 12 horas.\n\n"
                f"Para iniciar un nuevo trámite use /permiso."
            )
        
        # Limpiar timers
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Recordatorios de la última hora."""
    try:
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - JALISCO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Envía la foto del comprobante para validar."
        )
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    """Inicia el timer de 12 horas con avisos 60/30/10 min antes del fin."""
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id}")
        # Dormir 11 horas (660 min)
        await asyncio.sleep(11 * 3600)

        # Aviso a 60 min
        if folio not in timers_activos: return
        await enviar_recordatorio(user_id, folio, 60)
        await asyncio.sleep(30 * 60)

        # Aviso a 30 min
        if folio not in timers_activos: return
        await enviar_recordatorio(user_id, folio, 30)
        await asyncio.sleep(20 * 60)

        # Aviso a 10 min
        if folio not in timers_activos: return
        await enviar_recordatorio(user_id, folio, 10)
        await asyncio.sleep(10 * 60)

        # Eliminar si sigue activo
        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    # Agregar folio a la lista del usuario
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer iniciado para folio {folio}, total timers activos: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        # Remover de estructuras de datos
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}")

def limpiar_timer_folio(folio: str):
    """Limpia todas las referencias de un folio tras expirar"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    """Obtiene todos los folios activos de un usuario"""
    return user_folios.get(user_id, [])

# ============ COORDENADAS Y FUNCIONES PDF ============
coords_jalisco = {
    "folio": (960, 391, 14, (0, 0, 0)),
    "marca": (330, 361, 14, (0, 0, 0)),
    "serie": (960, 361, 14, (0, 0, 0)),
    "linea": (330, 391, 14, (0, 0, 0)),
    "motor": (300, 260, 14, (0, 0, 0)),
    "anio": (330, 421, 14, (0, 0, 0)),
    "color": (330, 451, 14, (0, 0, 0)),
    "nombre": (330, 331, 14, (0, 0, 0)),
    # FECHAS
    "fecha_exp": (120, 350, 14, (0, 0, 0)),
    "fecha_exp_completa": (120, 370, 14, (0, 0, 0)),
    "fecha_ven": (310, 605, 90, (0, 0, 0))
}

def generar_qr_dinamico_jalisco(folio):
    try:
        url_directa = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1
        )
        qr.add_data(url_directa)
        qr.make(fit=True)

        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR JALISCO] Generado para folio {folio} -> {url_directa}")
        return img_qr, url_directa
        
    except Exception as e:
        print(f"[ERROR QR JALISCO] {e}")
        return None, None

def obtener_folio_representativo():
    """Obtiene folio representativo, manteniendo persistencia entre reinicios"""
    try:
        with open("folio_representativo.txt") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        folio_inicial = 501997
        with open("folio_representativo.txt", "w") as f:
            f.write(str(folio_inicial))
        print(f"[REPRESENTATIVO] Archivo creado con valor inicial: {folio_inicial}")
        return folio_inicial
    except Exception as e:
        print(f"[ERROR] Leyendo folio representativo: {e}")
        return 501997

def incrementar_folio_representativo(folio_actual):
    """Incrementa y guarda el folio representativo"""
    try:
        nuevo = folio_actual + 1
        with open("folio_representativo.txt", "w") as f:
            f.write(str(nuevo))
        print(f"[REPRESENTATIVO] Incrementado de {folio_actual} a {nuevo}")
        return nuevo
    except Exception as e:
        print(f"[ERROR] Incrementando folio representativo: {e}")
        return folio_actual + 1

def generar_codigo_ine(contenido, ruta_salida):
    """Genera código PDF417 estilo INE"""
    try:
        codes = pdf417gen.encode(contenido, columns=6, security_level=5)
        image = pdf417gen.render_image(codes)
        image.save(ruta_salida)
        print(f"[PDF417] Código generado: {ruta_salida}")
    except Exception as e:
        print(f"[ERROR] Generando PDF417: {e}")
        img_fallback = Image.new('RGB', (200, 50), color='white')
        img_fallback.save(ruta_salida)

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ============ PDF PRINCIPAL (COMPLETO) ============
def generar_pdf_principal(datos: dict) -> str:
    """Genera el PDF principal completo con todos los datos"""
    fol = datos["folio"]
    fecha_exp = datos["fecha_exp"]
    fecha_ven = datos["fecha_ven"]
    
    # === FECHA Y HORA ACTUAL DE MÉXICO ===
    zona_mexico = pytz.timezone("America/Mexico_City")
    _ = datetime.now(zona_mexico)
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_jalisco1.pdf")
    
    try:
        doc = fitz.open(PLANTILLA_PDF)
        pg = doc[0]

        # Campos
        for campo in ["marca", "linea", "anio", "serie", "nombre", "color"]:
            if campo in coords_jalisco and campo in datos:
                x, y, s, col = coords_jalisco[campo]
                pg.insert_text((x, y), datos.get(campo, ""), fontsize=s, color=col)

        # Fecha de vencimiento
        pg.insert_text(coords_jalisco["fecha_ven"][:2], fecha_ven.strftime("%d/%m/%Y"),
                       fontsize=coords_jalisco["fecha_ven"][2], color=coords_jalisco["fecha_ven"][3])

        # Folio
        pg.insert_text((930, 391), fol, fontsize=14, color=(0, 0, 0))

        # Fecha actual emisión
        fecha_actual_str = fecha_exp.strftime("%d/%m/%Y")
        pg.insert_text((478, 804), fecha_actual_str, fontsize=32, color=(0, 0, 0))

        # Folio representativo
        fol_rep = obtener_folio_representativo()
        pg.insert_text((337, 804), str(fol_rep), fontsize=32, color=(0, 0, 0))
        pg.insert_text((650, 204), str(fol_rep), fontsize=45, color=(0, 0, 0))
        incrementar_folio_representativo(fol_rep)

        # Folio con asteriscos
        pg.insert_text((910, 620), f"*{fol}*", fontsize=30, color=(0, 0, 0), fontname="Courier")
        pg.insert_text((1083, 800), "DIGITAL", fontsize=14, color=(0, 0, 0))

        # PDF417 estilo INE
        contenido_ine = f"""FOLIO:{fol}
MARCA:{datos.get('marca', '')}
LINEA:{datos.get('linea', '')}
ANIO:{datos.get('anio', '')}
SERIE:{datos.get('serie', '')}
MOTOR:{datos.get('motor', '')}"""
        ine_img_path = os.path.join(OUTPUT_DIR, f"{fol}_inecode.png")
        generar_codigo_ine(contenido_ine, ine_img_path)
        pg.insert_image(fitz.Rect(937.65, 75, 1168.955, 132),
                        filename=ine_img_path, keep_proportion=False, overlay=True)

        # QR dinámico
        img_qr, url_qr = generar_qr_dinamico_jalisco(fol)
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            x_qr = coords_qr_dinamico["x"]
            y_qr = coords_qr_dinamico["y"]
            ancho_qr = coords_qr_dinamico["ancho"]
            alto_qr = coords_qr_dinamico["alto"]
            pg.insert_image(
                fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
                pixmap=qr_pix,
                overlay=True
            )
            print(f"[QR JALISCO] Insertado en ({x_qr}, {y_qr}) -> {url_qr}")

        doc.save(out)
        doc.close()
        print(f"[PDF] Generado exitosamente: {out}")
    except Exception as e:
        print(f"[ERROR] Generando PDF principal: {e}")
        doc_fallback = fitz.open()
        page = doc_fallback.new_page()
        page.insert_text((50, 50), f"ERROR - Folio: {fol}", fontsize=12)
        doc_fallback.save(out)
        doc_fallback.close()
    
    return out

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    """Genera el PDF simple con fecha+hora y serie"""
    try:
        doc = fitz.open(PLANTILLA_BUENO)
        page = doc[0]
        fecha_hora_str = fecha.strftime("%d/%m/%Y %H:%M")
        page.insert_text((380, 195), fecha_hora_str, fontsize=10, fontname="helv", color=(0, 0, 0))
        page.insert_text((380, 290), serie, fontsize=10, fontname="helv", color=(0, 0, 0))
        filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
        doc.save(filename)
        doc.close()
        return filename
    except Exception as e:
        print(f"[ERROR] Generando PDF bueno: {e}")
        return None

# ------------ HANDLERS PRINCIPALES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    frases_start = [
        "🏛️ BIENVENIDO AL SISTEMA DIGITAL DEL ESTADO DE JALISCO\n"
        "Plataforma oficial para la gestión de permisos de circulación vehicular\n\n"
        "📋 Inversión por servicio: Tarifa oficial establecida\n"
        "⏰ Plazo para liquidación: 12 horas a partir de la emisión\n"
        "💳 Modalidades de pago: Transferencia bancaria y OXXO\n\n"
        "Para iniciar use /permiso\n\n"
        "⚠️ Su folio se elimina automáticamente si no se paga en 12 horas.",
        
        "🌟 SISTEMA GUBERNAMENTAL DE JALISCO - SERVICIO DIGITAL\n"
        "💰 Concepto: Permiso temporal de circulación\n"
        "🕐 Tiempo disponible para pago: 12 horas (720 min)\n"
        "🏪 Puntos de pago autorizados: Red OXXO y transferencias bancarias\n\n"
        "Comando: /permiso"
    ]
    await message.answer(random.choice(frases_start))

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Verificar folios activos del usuario
    folios_activos = obtener_folios_usuario(message.from_user.id)
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS EN PROCESO: {', '.join(folios_activos)}\n(Cada expediente tiene su cronómetro independiente de 12 horas)"

    frases_inicio = [
        f"🚗 SOLICITUD DE PERMISO DE CIRCULACIÓN - ESTADO DE JALISCO\n\n"
        f"💰 Inversión requerida: Según tarifa oficial\n"
        f"⏰ Plazo para completar el pago: 12 horas\n\n"
        f"Al continuar, acepta que su folio será eliminado si no paga en el tiempo establecido."
        f"{mensaje_folios}\n\n"
        f"Primer paso: Indique la MARCA del vehículo:",
    ]
    await message.answer(random.choice(frases_inicio))
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    if not marca or len(marca) < 2:
        await message.answer("⚠️ Proporcione una MARCA válida (mínimo 2 caracteres):")
        return
    await state.update_data(marca=marca)
    await message.answer("Indique la LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    if not linea:
        await message.answer("⚠️ Proporcione la LÍNEA/MODELO del vehículo:")
        return
    await state.update_data(linea=linea)
    await message.answer("Indique el AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Formato inválido. Use 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("Indique el NÚMERO DE SERIE del vehículo:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5 or len(serie) > 25:
        await message.answer("⚠️ Serie inválida (5 a 25 caracteres). Intente de nuevo:")
        return
    await state.update_data(serie=serie)
    await message.answer("Indique el NÚMERO DE MOTOR del vehículo:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    if len(motor) < 5 or len(motor) > 25:
        await message.answer("⚠️ Motor inválido (5 a 25 caracteres). Intente de nuevo:")
        return
    await state.update_data(motor=motor)
    await message.answer("Indique el COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    if not color or len(color) > 20:
        await message.answer("⚠️ Color inválido. Máximo 20 caracteres:")
        return
    await state.update_data(color=color)
    await message.answer("Indique el NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()

    if len(nombre) < 5 or len(nombre) > 60 or len(nombre.split()) < 2:
        await message.answer("⚠️ Nombre completo inválido (mínimo nombre y apellido, máx. 60 caracteres):")
        return
    
    datos["nombre"] = nombre

    # Fechas
    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy
    datos["fecha_ven"] = fecha_ven

    # Folio provisional SOLO para mostrar (el definitivo lo fija el insert con reintentos)
    datos["folio"] = generar_folio_jalisco_sync()
    print(f"[DEBUG] Folio provisional mostrado: {datos['folio']}")

    try:
        await message.answer(
            f"🔄 Generando documentación...\n"
            f"Folio (provisional): {datos['folio']}\n"
            f"Titular: {nombre}"
        )
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

        await message.answer_document(
            FSInputFile(p1),
            caption=f"📋 PERMISO DE CIRCULACIÓN - JALISCO\nFolio: {datos['folio']}\nVigencia: 30 días"
        )
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"🧾 Documento complementario\nFolio: {datos['folio']}\nSerie: {datos['serie']}"
            )

        # Guardar en DB con reintento/colisión (aquí se asegura el folio definitivo)
        guardado_exitoso = await guardar_folio_con_reintento(datos, message.from_user.id, message.from_user.username)
        if not guardado_exitoso:
            await message.answer("❌ No se pudo registrar el folio. Intente de nuevo con /permiso")
            await state.clear()
            return

        # Guardar borrador (best-effort)
        try:
            supabase.table("borradores_registros").insert({
                "folio": datos["folio"],
                "entidad": "Jalisco",
                "numero_serie": datos["serie"],
                "marca": datos["marca"],
                "linea": datos["linea"],
                "numero_motor": datos["motor"],
                "anio": datos["anio"],
                "color": datos["color"],
                "fecha_expedicion": hoy.isoformat(),
                "fecha_vencimiento": fecha_ven.isoformat(),
                "contribuyente": datos["nombre"],
                "estado": "PENDIENTE",
                "user_id": message.from_user.id
            }).execute()
        except Exception as e:
            print(f"[WARN] Error guardando en borradores: {e}")

        # Iniciar timer de 12 horas
        await iniciar_timer_eliminacion(message.from_user.id, datos['folio'])

        # Instrucciones
        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"Folio: {datos['folio']}\n"
            f"Monto: {PRECIO_PERMISO} pesos\n"
            f"Tiempo límite: 12 horas\n\n"
            "🏦 TRANSFERENCIA (ejemplo):\n"
            "• Institución: SPIN BY OXXO\n"
            "• Titular: GUILLERMO S.R\n"
            "• Cuenta: 728969000048442454\n"
            f"• Concepto: Permiso {datos['folio']}\n\n"
            "🏪 OXXO (ejemplo):\n"
            "• Referencia: 2242170180214090\n"
            "• Titular: GUILLERMO S.R\n\n"
            "📸 Envía la foto del comprobante para validar.\n"
            "⚠️ Si no pagas en 12 horas, el folio se elimina automáticamente."
        )

    except Exception as e:
        await message.answer(f"❌ Error generando documentación: {str(e)}")
        print(f"Error: {e}")
    finally:
        await state.clear()

# ------------ CÓDIGO SECRETO ADMIN MEJORADO ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) > 4:
        folio_admin = texto[4:]
        folio_encontrado = False
        user_con_folio = None
        
        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
            folio_encontrado = True
        
        if folio_encontrado:
            cancelar_timer_folio(folio_admin)
            try:
                supabase.table("folios_registrados").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
                supabase.table("borradores_registros").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
            except Exception as e:
                print(f"Error actualizando BD para folio {folio_admin}: {e}")
            
            await message.answer(
                f"✅ VALIDACIÓN ADMINISTRATIVA OK\n"
                f"Folio: {folio_admin}\n"
                f"Timer cancelado y estado actualizado."
            )
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - JALISCO\n"
                    f"Folio: {folio_admin}\n"
                    f"Tu permiso está activo para circular."
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            await message.answer(
                f"❌ FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n"
                f"Folio consultado: {folio_admin}"
            )
    else:
        await message.answer(
            "⚠️ Formato: SERO[número_de_folio]\n"
            "Ejemplo: SERO900345876"
        )

# Handler para recibir comprobantes de pago (imágenes)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes de pago vinculados a tu cuenta.\n"
                "Para iniciar uno nuevo usa /permiso"
            )
            return
        
        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 Tienes varios folios activos:\n\n{lista_folios}\n\n"
                f"Responde con el NÚMERO DE FOLIO al que corresponde este comprobante."
            )
            return
        
        # Solo un folio activo
        folio = folios_usuario[0]
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"Folio: {folio}\n"
                f"Timer detenido. Se está verificando tu pago."
            )
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"Folio: {folio}\n"
                f"⚠️ Hubo un detalle guardando el estado, pero tu comprobante quedó registrado."
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            "❌ Error procesando el comprobante. Intenta enviar la foto nuevamente."
        )

# Handler para cuando el usuario especifica el folio para el comprobante
@dp.message(lambda message: message.from_user.id in pending_comprobantes and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folio_especificado = message.text.strip().upper()
        folios_usuario = obtener_folios_usuario(user_id)
        
        if folio_especificado not in folios_usuario:
            await message.answer(
                "❌ Ese folio no está entre tus expedientes activos.\n"
                "Responde con uno de tu lista actual."
            )
            return
        
        cancelar_timer_folio(folio_especificado)
        del pending_comprobantes[user_id]
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            await message.answer(
                f"✅ Comprobante asociado.\n"
                f"Folio: {folio_especificado}\n"
                f"Timer detenido. Se está verificando tu pago."
            )
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ Folio confirmado: {folio_especificado}\n"
                f"Timer detenido. Validación en proceso."
            )
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer("❌ Error procesando el folio especificado. Intenta de nuevo.")

# Comando para ver folios activos
@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ No tienes expedientes activos.\n"
                "Para tramitar un nuevo permiso usa /permiso"
            )
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                transcurrido = int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, TOTAL_MINUTOS_TIMER - transcurrido)
                lista_folios.append(f"• {folio} ({tiempo_restante} min restantes)")
            else:
                lista_folios.append(f"• {folio} (cronómetro detenido)")
        
        await message.answer(
            f"📋 TUS EXPEDIENTES ACTIVOS ({len(folios_usuario)})\n\n" +
            '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio tiene cronómetro independiente de 12 horas.\n"
            f"📸 Envía la foto del comprobante para validar."
        )
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer("❌ Error consultando expedientes activos. Intenta nuevamente.")

# Handler para preguntas sobre costo/precio
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    try:
        await message.answer(
            "💰 Costo según tarifa oficial.\n"
            "⏰ Límite de pago: 12 horas.\n"
            "📋 Vigencia del permiso: 30 días.\n"
            "Para iniciar: /permiso"
        )
    except Exception as e:
        print(f"[ERROR] responder_costo: {e}")
        await message.answer("💰 Costo según tarifa oficial. Usa /permiso para tramitar.")

@dp.message()
async def fallback(message: types.Message):
    respuestas = [
        "🏛️ Sistema Digital Jalisco. Para tramitar tu permiso: /permiso",
        "📋 Servicio en línea. Comando: /permiso",
        "⚡ Genera tu documento oficial con /permiso",
        "🚗 Permisos vehiculares de Jalisco. Inicia con /permiso"
    ]
    await message.answer(random.choice(respuestas))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    """Mantiene el bot activo con pings periódicos"""
    while True:
        await asyncio.sleep(600)  # 10 minutos
        print("[HEARTBEAT] Sistema Jalisco activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    try:
        # Inicializar cursor de folios desde DB/local
        await inicializar_folio_cursor()

        # Configurar webhook
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            webhook_url = f"{BASE_URL}/webhook"
            await bot.set_webhook(webhook_url, allowed_updates=["message"])
            print(f"[WEBHOOK] Configurado: {webhook_url}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Modo sin webhook")
        print("[SISTEMA] ¡Sistema Digital Jalisco iniciado correctamente!")
        yield
    except Exception as e:
        print(f"[ERROR CRÍTICO] Iniciando sistema: {e}")
        yield
    finally:
        print("[CIERRE] Cerrando sistema...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Jalisco Digital", version="2.0")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    try:
        return {
            "ok": True, 
            "bot": "Jalisco Permisos Sistema", 
            "status": "running",
            "version": "2.0",
            "entidad": "Jalisco",
            "vigencia": "30 días",
            "timer_eliminacion": "12 horas",
            "active_timers": len(timers_activos),
            "folio_cursor_actual": _folio_cursor,
            "folio_inicio": FOLIO_INICIO,
            "folio_fin": FOLIO_FIN,                # <- agregado para diagnosticar
            "continuidad_folios": "Consecutivo 9 dígitos desde Supabase/local"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
        
@app.get("/status")
async def status_detail():
    """Endpoint de diagnóstico detallado"""
    try:
        return {
            "sistema": "Jalisco Digital v2.0 - Folios consecutivos",
            "entidad": "Jalisco",
            "vigencia_dias": 30,
            "tiempo_eliminacion": "12 horas con avisos 60/30/10",
            "total_timers_activos": len(timers_activos),
            "folios_con_timer": list(timers_activos.keys()),
            "usuarios_con_folios": len(user_folios),
            "continuidad": "Folios desde último en DB; +1 garantizado con lock",
            "folio_cursor": _folio_cursor,
            "timestamp": datetime.now().isoformat(),
            "status": "Operacional"
        }
    except Exception as e:
        return {"error": str(e), "status": "Error"}

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] Folios consecutivos desde {FOLIO_INICIO} - Auto-eliminación: 12 horas")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
