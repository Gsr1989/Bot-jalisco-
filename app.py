from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz
import pytz
from PIL import Image
from io import BytesIO
import json
import re
import qrcode

# PDF417
try:
    from pdf417 import encode as pdf417_encode, render_image as pdf417_render
    PDF417_DISPONIBLE = True
    print("[PDF417] Librería disponible ✅")
except ImportError:
    PDF417_DISPONIBLE = False
    print("[PDF417] Librería NO disponible, usando QR fallback")

# ------------ CONFIG ------------
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL     = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR   = "documentos"
PLANTILLA_PDF   = "jalisco1.pdf"
PLANTILLA_BUENO = "jalisco.pdf"

PRECIO_PERMISO      = 250
PRECIO_FIJO_PAGINA2 = 1080

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

URL_CONSULTA_BASE = "https://serviciodigital-jaliscogobmx.onrender.com"

# QR principal (cuadrado, sin cambios)
coords_qr_dinamico = {"x": 966, "y": 603, "ancho": 140, "alto": 140}

# Rectángulo donde va el PDF417
RECT_PDF417 = fitz.Rect(932.65, 807, 1141.395, 852.127)

# ------------ PLANTILLAS EN MEMORIA ------------
_plantilla1_bytes: bytes | None = None
_plantilla2_bytes: bytes | None = None

def _cargar_plantillas():
    global _plantilla1_bytes, _plantilla2_bytes
    with open(PLANTILLA_PDF, "rb") as f:
        _plantilla1_bytes = f.read()
    with open(PLANTILLA_BUENO, "rb") as f:
        _plantilla2_bytes = f.read()
    print("[PLANTILLAS] Cargadas en memoria ✅")

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT con timeout 300s ------------
session_bot = AiohttpSession(timeout=300)
bot         = Bot(token=BOT_TOKEN, session=session_bot)
storage     = MemoryStorage()
dp          = Dispatcher(storage=storage)

# ============ FOLIOS CONSECUTIVOS ============
PREFIJOS_VALIDOS = {
    "1": 980000000,
    "2": 890000000,
    "3": 780000000,
}

_folio_cursors = {}
_folio_lock    = asyncio.Lock()

def _leer_cursors_local():
    try:
        with open("folio_cursors.json") as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except Exception:
        return {}

def _guardar_cursors_local(cursors: dict):
    try:
        with open("folio_cursors.json", "w") as f:
            json.dump(cursors, f)
    except Exception as e:
        print(f"[WARN] No se pudo persistir cursors: {e}")

def _leer_ultimo_folio_por_prefijo(prefijo: str):
    """Síncrono — usar con asyncio.to_thread."""
    try:
        base = PREFIJOS_VALIDOS[prefijo]
        resp = (
            supabase.table("folios_registrados")
            .select("folio")
            .gte("folio", str(base))
            .lt("folio", str(base + 100000000))
            .order("folio", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            ultimo = int(resp.data[0]["folio"])
            print(f"[FOLIO][DB] Último folio prefijo {prefijo}: {ultimo}")
            return ultimo
        return base - 1
    except Exception as e:
        print(f"[ERROR] Consultando folios prefijo {prefijo}: {e}")
        return PREFIJOS_VALIDOS[prefijo] - 1

async def inicializar_folio_cursors():
    global _folio_cursors
    cursors_local = _leer_cursors_local()
    for prefijo in PREFIJOS_VALIDOS:
        ultimo_db    = await asyncio.to_thread(_leer_ultimo_folio_por_prefijo, prefijo)
        ultimo_local = cursors_local.get(prefijo)
        if ultimo_local is not None and ultimo_local > ultimo_db:
            _folio_cursors[prefijo] = ultimo_local
            print(f"[FOLIO] Prefijo {prefijo} desde local: {ultimo_local}")
        else:
            _folio_cursors[prefijo] = ultimo_db
            print(f"[FOLIO] Prefijo {prefijo} desde DB: {ultimo_db}")
    _guardar_cursors_local(_folio_cursors)

async def generar_folio_con_prefijo(prefijo: str) -> str:
    global _folio_cursors
    if prefijo not in PREFIJOS_VALIDOS:
        prefijo = "1"
    async with _folio_lock:
        base   = PREFIJOS_VALIDOS[prefijo]
        limite = base + 100000000
        _folio_cursors[prefijo] += 1
        if _folio_cursors[prefijo] >= limite:
            _folio_cursors[prefijo] = base
        _guardar_cursors_local(_folio_cursors)
        folio = f"{_folio_cursors[prefijo]:09d}"
        print(f"[FOLIO] Generado prefijo {prefijo}: {folio}")
        return folio

def _sb_insertar_folio(datos: dict, user_id: int, username: str):
    """Síncrono — usar con asyncio.to_thread."""
    supabase.table("folios_registrados").insert({
        "folio":             datos["folio"],
        "marca":             datos["marca"],
        "linea":             datos["linea"],
        "anio":              datos["anio"],
        "numero_serie":      datos["serie"],
        "numero_motor":      datos["motor"],
        "color":             datos["color"],
        "nombre":            datos["nombre"],
        "fecha_expedicion":  datos["fecha_exp"].date().isoformat(),
        "fecha_vencimiento": datos["fecha_ven"].date().isoformat(),
        "entidad":  "Jalisco",
        "estado":   "PENDIENTE",
        "user_id":  user_id,
        "username": username or "Sin username",
    }).execute()

def _sb_insertar_borrador(datos: dict, user_id: int):
    """Síncrono — usar con asyncio.to_thread."""
    hoy       = datos["fecha_exp"]
    fecha_ven = datos["fecha_ven"]
    supabase.table("borradores_registros").insert({
        "folio":             datos["folio"],
        "entidad":           "Jalisco",
        "numero_serie":      datos["serie"],
        "marca":             datos["marca"],
        "linea":             datos["linea"],
        "numero_motor":      datos["motor"],
        "anio":              datos["anio"],
        "color":             datos["color"],
        "fecha_expedicion":  hoy.isoformat(),
        "fecha_vencimiento": fecha_ven.isoformat(),
        "contribuyente":     datos["nombre"],
        "estado":            "PENDIENTE",
        "user_id":           user_id,
    }).execute()

async def guardar_folio_con_reintento(datos: dict, user_id: int, username: str, prefijo="1") -> bool:
    for intento in range(100_000_000):
        if "folio" not in datos or not re.fullmatch(r"\d{9}", str(datos.get("folio", ""))):
            datos["folio"] = await generar_folio_con_prefijo(prefijo)
        try:
            await asyncio.to_thread(_sb_insertar_folio, datos, user_id, username)
            print(f"[ÉXITO] ✅ Folio {datos['folio']} guardado (intento {intento+1})")
            return True
        except Exception as e:
            em = str(e).lower()
            if "duplicate" in em or "unique constraint" in em or "23505" in em:
                print(f"[DUPLICADO] {datos['folio']} existe, reintentando ({intento+1})")
                datos["folio"] = None
                await asyncio.sleep(0.1)
                continue
            print(f"[ERROR BD] {e}")
            return False
    return False

# ============ FOLIOS PÁGINA 2 ============
def _leer_folios_pagina2():
    try:
        with open("folios_pagina2.json") as f:
            return json.load(f)
    except Exception:
        return {
            "referencia_pago":   273312001734,
            "num_autorizacion":  370803,
            "folio_seguimiento": "GZUdr61oqv2",
            "linea_captura":     41340816,
        }

def _guardar_folios_pagina2(folios: dict):
    try:
        with open("folios_pagina2.json", "w") as f:
            json.dump(folios, f)
    except Exception as e:
        print(f"[WARN] No se pudo persistir folios página 2: {e}")

def _incrementar_sufijo_alfabetico(sufijo: str) -> str:
    chars = list(sufijo)
    for i in range(len(chars)-1, -1, -1):
        if chars[i] == 'z':
            chars[i] = 'a'
        else:
            chars[i] = chr(ord(chars[i]) + 1)
            break
    return ''.join(chars)

def _incrementar_alfanumerico(codigo: str) -> str:
    match = re.match(r'(\D*)(\d+)([a-z]+)(\d+)$', codigo)
    if match:
        prefijo = match.group(1)
        numero  = match.group(2)
        letras  = match.group(3)
        digito  = int(match.group(4)) + 1
        if digito > 9:
            digito = 0
            letras = _incrementar_sufijo_alfabetico(letras)
        return f"{prefijo}{numero}{letras}{digito}"
    return codigo[:-1] + str((int(codigo[-1]) + 1) % 10)

def generar_folios_pagina2() -> dict:
    folios = _leer_folios_pagina2()
    folios["referencia_pago"]   += 1
    folios["num_autorizacion"]  += 1
    folios["folio_seguimiento"]  = _incrementar_alfanumerico(folios["folio_seguimiento"])
    folios["linea_captura"]     += 1
    _guardar_folios_pagina2(folios)
    print(f"[PÁGINA 2] Ref={folios['referencia_pago']}, Auth={folios['num_autorizacion']}, "
          f"Seg={folios['folio_seguimiento']}, Linea={folios['linea_captura']}")
    return folios

# ============ FOLIO REPRESENTATIVO ============
def obtener_folio_representativo():
    try:
        with open("folio_representativo.txt") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        folio_inicial = 21385
        with open("folio_representativo.txt", "w") as f:
            f.write(str(folio_inicial))
        return folio_inicial
    except Exception as e:
        print(f"[ERROR] Leyendo folio representativo: {e}")
        return 21385

def incrementar_folio_representativo(folio_actual):
    try:
        nuevo = folio_actual + 1
        with open("folio_representativo.txt", "w") as f:
            f.write(str(nuevo))
        return nuevo
    except Exception as e:
        print(f"[ERROR] Incrementando folio representativo: {e}")
        return folio_actual + 1

# ============ TIMERS 36H ============
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").delete().eq("folio", folio).execute(),
            supabase.table("borradores_registros").delete().eq("folio", folio).execute(),
        ))
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - ESTADO DE JALISCO\n\n"
                f"El folio {folio} ha sido eliminado por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - JALISCO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen).\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado folio {folio}, usuario {user_id} (36h)")
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            print(f"[TIMER] Expirado folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer 36h iniciado folio {folio}, total: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        print(f"[SISTEMA] Timer cancelado folio {folio}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ============ COORDENADAS PDF ============
coords_jalisco = {
    "marca":     (340, 332, 14, (0,0,0)),
    "serie":     (920, 332, 14, (0,0,0)),
    "linea":     (340, 360, 14, (0,0,0)),
    "anio":      (340, 389, 14, (0,0,0)),
    "color":     (340, 418, 14, (0,0,0)),
    "nombre":    (340, 304, 14, (0,0,0)),
    "fecha_ven": (285, 570, 90, (0,0,0)),
}

coords_pagina2 = {
    "referencia_pago":   (380, 123, 10, (0,0,0)),
    "num_autorizacion":  (380, 147, 10, (0,0,0)),
    "total_pagado":      (380, 170, 10, (0,0,0)),
    "folio_seguimiento": (380, 243, 10, (0,0,0)),
    "linea_captura":     (380, 265, 10, (0,0,0)),
}

# ============ QR PRINCIPAL (original con qrcode) ============
def _generar_qr_jalisco(folio: str):
    """Síncrono — usar con asyncio.to_thread."""
    try:
        url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color=(220,220,220)).convert("RGB")
        print(f"[QR] Generado para folio {folio}")
        return img
    except Exception as e:
        print(f"[ERROR QR] {e}")
        return None

# ============ PDF417 RECTANGULAR (original con qrcode fallback) ============
def _generar_pdf417(datos: dict) -> Image.Image | None:
    """
    Genera un PDF417 con todos los datos del vehículo separados por espacio.
    Síncrono — usar con asyncio.to_thread.
    """
    texto = (
        f"FOLIO {datos['folio']} "
        f"MARCA {datos['marca']} "
        f"LINEA {datos['linea']} "
        f"ANIO {datos['anio']} "
        f"SERIE {datos['serie']} "
        f"MOTOR {datos['motor']} "
        f"COLOR {datos['color']} "
        f"TITULAR {datos['nombre']}"
    )

    ancho_pts = int(RECT_PDF417.x1 - RECT_PDF417.x0)
    alto_pts  = int(RECT_PDF417.y1 - RECT_PDF417.y0)

    if PDF417_DISPONIBLE:
        try:
            codes = pdf417_encode(texto, columns=8, security_level=2)
            img   = pdf417_render(codes, scale=2, ratio=2)
            img   = img.convert("RGB")
            img   = img.resize((ancho_pts * 4, alto_pts * 4), Image.LANCZOS)
            print(f"[PDF417] Generado: {texto[:60]}...")
            return img
        except Exception as e:
            print(f"[ERROR PDF417] {e} — usando QR fallback")

    # Fallback: QR estirado con qrcode
    try:
        qr = qrcode.QRCode(version=4, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=2, border=1)
        qr.add_data(texto)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        img = img.resize((ancho_pts * 4, alto_pts * 4), Image.NEAREST)
        print(f"[QR FALLBACK PDF417] Generado para folio {datos['folio']}")
        return img
    except Exception as e:
        print(f"[ERROR QR FALLBACK] {e}")
        return None

# ============ FSM ============
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    nombre = State()

# ============ GENERACIÓN PDF (síncrono, llamar con to_thread) ================
def _generar_pdf_unificado(datos: dict) -> str:
    fol       = datos["folio"]
    fecha_exp = datos["fecha_exp"]
    fecha_ven = datos["fecha_ven"]

    zona_mexico = pytz.timezone("America/Mexico_City")
    ahora_cdmx  = datetime.now(zona_mexico)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_completo.pdf")

    try:
        # Usar bytes en caché — sin I/O de disco
        doc1 = fitz.open(stream=_plantilla1_bytes, filetype="pdf")
        pg1  = doc1[0]

        # ── Datos del vehículo ──
        for campo in ["marca", "linea", "anio", "serie", "nombre", "color"]:
            if campo in coords_jalisco and campo in datos:
                x, y, s, col = coords_jalisco[campo]
                pg1.insert_text((x, y), datos[campo], fontsize=s, color=col, fontname="hebo")

        pg1.insert_text(
            coords_jalisco["fecha_ven"][:2],
            fecha_ven.strftime("%d/%m/%Y"),
            fontsize=coords_jalisco["fecha_ven"][2],
            color=coords_jalisco["fecha_ven"][3]
        )

        pg1.insert_text((860, 364), fol, fontsize=14, color=(0,0,0), fontname="hebo")
        pg1.insert_text((475, 830), fecha_exp.strftime("%d/%m/%Y"), fontsize=32, color=(0,0,0), fontname="hebo")

        fol_rep      = obtener_folio_representativo()
        folio_grande = f"4A-DVM/{fol_rep}"
        pg1.insert_text((240, 830), folio_grande, fontsize=32, color=(0,0,0), fontname="hebo")
        pg1.insert_text((480, 182), folio_grande, fontsize=63, color=(0,0,0), fontname="hebo")

        folio_chico = f"DVM-{fol_rep}   {ahora_cdmx.strftime('%d/%m/%Y')}  {ahora_cdmx.strftime('%H:%M:%S')}"
        pg1.insert_text((915, 760), folio_chico, fontsize=14, color=(0,0,0), fontname="hebo")

        incrementar_folio_representativo(fol_rep)

        pg1.insert_text((935, 600), f"*{fol}*", fontsize=30, color=(0,0,0), fontname="Courier")
        pg1.insert_text((915, 775), "EXPEDICION: VENTANILLA 32", fontsize=12, color=(0,0,0), fontname="hebo")

        # ── QR cuadrado (posición original) ──
        img_qr = _generar_qr_jalisco(fol)
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            pg1.insert_image(
                fitz.Rect(
                    coords_qr_dinamico["x"],
                    coords_qr_dinamico["y"],
                    coords_qr_dinamico["x"] + coords_qr_dinamico["ancho"],
                    coords_qr_dinamico["y"] + coords_qr_dinamico["alto"]
                ),
                pixmap=fitz.Pixmap(buf.read()),
                overlay=True
            )
            print("[QR] Insertado en posición original")

        # ── PDF417 rectangular ──
        img_pdf417 = _generar_pdf417(datos)
        if img_pdf417:
            buf2 = BytesIO()
            img_pdf417.save(buf2, format="PNG")
            buf2.seek(0)
            pg1.insert_image(RECT_PDF417, pixmap=fitz.Pixmap(buf2.read()), overlay=True)
            print("[PDF417] Insertado en rect rectangular")

        # ── Página 2 — bytes en caché ──
        doc2 = fitz.open(stream=_plantilla2_bytes, filetype="pdf")
        pg2  = doc2[0]

        pg2.insert_text((380, 195), fecha_exp.strftime("%d/%m/%Y %H:%M"), fontsize=10, fontname="helv", color=(0,0,0))
        pg2.insert_text((380, 290), datos["serie"], fontsize=10, fontname="helv", color=(0,0,0))

        fp2 = generar_folios_pagina2()
        pg2.insert_text(coords_pagina2["referencia_pago"][:2],   str(fp2["referencia_pago"]),
                        fontsize=coords_pagina2["referencia_pago"][2],   color=coords_pagina2["referencia_pago"][3])
        pg2.insert_text(coords_pagina2["num_autorizacion"][:2],  str(fp2["num_autorizacion"]),
                        fontsize=coords_pagina2["num_autorizacion"][2],  color=coords_pagina2["num_autorizacion"][3])
        pg2.insert_text(coords_pagina2["total_pagado"][:2],      f"${PRECIO_FIJO_PAGINA2}.00 MN",
                        fontsize=coords_pagina2["total_pagado"][2],      color=coords_pagina2["total_pagado"][3])
        pg2.insert_text(coords_pagina2["folio_seguimiento"][:2], fp2["folio_seguimiento"],
                        fontsize=coords_pagina2["folio_seguimiento"][2], color=coords_pagina2["folio_seguimiento"][3])
        pg2.insert_text(coords_pagina2["linea_captura"][:2],     str(fp2["linea_captura"]),
                        fontsize=coords_pagina2["linea_captura"][2],     color=coords_pagina2["linea_captura"][3])

        doc_final = fitz.open()
        doc_final.insert_pdf(doc1)
        doc_final.insert_pdf(doc2)
        doc_final.save(out)
        doc_final.close()
        doc1.close()
        doc2.close()

        print(f"[PDF UNIFICADO] ✅ Generado: {out}")

    except Exception as e:
        print(f"[ERROR] Generando PDF: {e}")
        doc_fb = fitz.open()
        doc_fb.new_page().insert_text((50, 50), f"ERROR - Folio: {fol}", fontsize=12)
        doc_fb.save(out)
        doc_fb.close()

    return out

# ============ BACKGROUND: genera y manda PDF =================================
async def _generar_y_enviar_background(chat_id: int, datos: dict, user_id: int):
    try:
        fecha_ven = datos["fecha_ven"]

        pdf_path = await asyncio.to_thread(_generar_pdf_unificado, datos)

        folio_final = datos["folio"]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{folio_final}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio_final}")
        ]])

        await bot.send_document(
            chat_id,
            FSInputFile(pdf_path),
            caption=(
                f"📋 PERMISO DE CIRCULACIÓN - JALISCO\n"
                f"Folio: {folio_final}\nVigencia: 30 días ({fecha_ven.strftime('%d/%m/%Y')})\n\n"
                f"✅ Documento con 2 páginas unificadas\n⏰ TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        try:
            await asyncio.to_thread(_sb_insertar_borrador, datos, user_id)
        except Exception as e:
            print(f"[WARN] Error guardando borradores: {e}")

        await iniciar_timer_eliminacion(user_id, folio_final)

        await bot.send_message(
            user_id,
            "💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio_final}\n"
            f"💵 Monto: ${PRECIO_PERMISO}\n"
            "⏰ Tiempo límite: 36 horas\n\n"
            "🏦 TRANSFERENCIA:\n"
            "• Institución: SPIN BY OXXO\n"
            "• Titular: GUILLERMO S.R\n"
            "• Cuenta: 728969000048442454\n"
            f"• Concepto: Permiso {folio_final}\n\n"
            "🏪 OXXO:\n"
            "• Referencia: 2242170180214090\n"
            "• Titular: GUILLERMO S.R\n\n"
            "📸 Envía foto del comprobante para validar.\n"
            "⚠️ Sin pago en 36h el folio se elimina.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        print(f"[ERROR] _generar_y_enviar_background folio {datos.get('folio','?')}: {e}")
        try:
            await bot.send_message(
                user_id,
                f"❌ Error al generar el documento: {e}\n\nUse /chuleta para reintentar."
            )
        except Exception:
            pass

# ============ HANDLERS BOT ====================================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE JALISCO\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ Su folio será eliminado si no paga dentro del tiempo límite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    folios_activos = obtener_folios_usuario(message.from_user.id)

    if folios_activos:
        lineas = []
        for f in folios_activos:
            if f in timers_activos:
                mins = max(0, 2160 - int(
                    (datetime.now() - timers_activos[f]["start_time"]).total_seconds() / 60
                ))
                lineas.append(f"• {f}  ({mins//60}h {mins%60}min restantes)")
            else:
                lineas.append(f"• {f}  (sin timer)")

        botones = [
            [InlineKeyboardButton(text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")]
            for f in folios_activos
        ]
        kb_folios = InlineKeyboardMarkup(inline_keyboard=botones)

        await message.answer(
            f"📋 FOLIOS JALISCO ACTIVOS ({len(folios_activos)}):\n\n" +
            "\n".join(lineas) +
            "\n\nPuedes detener el timer de cualquier folio:",
            reply_markup=kb_folios
        )

    await message.answer(
        f"🚗 NUEVO PERMISO - ESTADO DE JALISCO\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        f"⏰ Plazo de pago: 36 horas\n\n"
        f"Primer paso: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Formato inválido. Use 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos            = await state.get_data()
    datos["nombre"]  = message.text.strip().upper()
    hoy              = datetime.now()
    datos["fecha_exp"] = hoy
    datos["fecha_ven"] = hoy + timedelta(days=30)
    await state.clear()

    ok = await guardar_folio_con_reintento(datos, message.from_user.id,
                                           message.from_user.username, "1")
    if not ok:
        await message.answer(
            "❌ No se pudo registrar el folio. Intenta de nuevo con /chuleta\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )
        return

    folio_final = datos["folio"]

    await message.answer(
        f"🔄 Generando documentación...\n"
        f"<b>Folio:</b> {folio_final}\n"
        f"<b>Titular:</b> {datos['nombre']}",
        parse_mode="HTML"
    )

    asyncio.create_task(
        _generar_y_enviar_background(message.chat.id, datos, message.from_user.id)
    )

# ============ CALLBACKS ADMIN =================================================

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if folio in timers_activos:
        user_con_folio = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
            ))
        except Exception as e:
            print(f"Error actualizando BD folio {folio}: {e}")
        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - JALISCO\n"
                f"Folio: {folio}\nTu permiso está activo.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando usuario: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        try:
            await asyncio.to_thread(lambda:
                supabase.table("folios_registrados").update(
                    {"estado": "TIMER_DETENIDO", "fecha_detencion": datetime.now().isoformat()}
                ).eq("folio", folio).execute()
            )
        except Exception as e:
            print(f"Error actualizando BD: {e}")
        await callback.answer("⏹️ Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\n\nFolio: {folio}\n"
            f"El timer de eliminación ha sido detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

# ============ ADMIN POR TEXTO (SERO) =========================================

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) <= 4:
        await message.answer(
            "⚠️ Formato: SERO[folio]\nEjemplo: SERO980000000\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )
        return
    folio_admin = texto[4:]
    if folio_admin in timers_activos:
        user_con_folio = timers_activos[folio_admin]["user_id"]
        cancelar_timer_folio(folio_admin)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio_admin).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio_admin).execute(),
            ))
        except Exception as e:
            print(f"Error actualizando BD folio {folio_admin}: {e}")
        await message.answer(
            f"✅ VALIDACIÓN OK\nFolio: {folio_admin}\nTimer cancelado.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - JALISCO\n"
                f"Folio: {folio_admin}\nTu permiso está activo.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando usuario: {e}")
    else:
        await message.answer(
            f"❌ Folio {folio_admin} no encontrado en timers activos.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

# ============ COMPROBANTE FOTO ===============================================

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id        = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes.\n\n📋 Para generar otro permiso use /chuleta"
            )
            return
        if len(folios_usuario) > 1:
            lista = "\n".join(f"• {f}" for f in folios_usuario)
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 Tienes varios folios activos:\n\n{lista}\n\n"
                f"Responde con el NÚMERO DE FOLIO para este comprobante.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            return
        folio = folios_usuario[0]
        cancelar_timer_folio(folio)
        now = datetime.now().isoformat()
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute(),
            supabase.table("borradores_registros").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute(),
        ))
        await message.answer(
            f"✅ Comprobante recibido.\n📄 Folio: {folio}\n⏹️ Timer detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            "❌ Error procesando comprobante. Intenta de nuevo.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )

@dp.message(lambda m: m.from_user.id in pending_comprobantes
            and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id   = message.from_user.id
        folio_esp = message.text.strip().upper()
        if folio_esp not in obtener_folios_usuario(user_id):
            await message.answer(
                "❌ Ese folio no está en tu lista activa.\n\n"
                "📋 Para generar otro permiso use /chuleta"
            )
            return
        cancelar_timer_folio(folio_esp)
        del pending_comprobantes[user_id]
        now = datetime.now().isoformat()
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio_esp).execute(),
            supabase.table("borradores_registros").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio_esp).execute(),
        ))
        await message.answer(
            f"✅ Comprobante asociado.\n📄 Folio: {folio_esp}\n⏹️ Timer detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"[ERROR] especificar_folio: {e}")
        pending_comprobantes.pop(message.from_user.id, None)
        await message.answer(
            "❌ Error. Intenta de nuevo.\n\n📋 Para generar otro permiso use /chuleta"
        )

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    folios_usuario = obtener_folios_usuario(message.from_user.id)
    if not folios_usuario:
        await message.answer(
            "ℹ️ No tienes folios pendientes.\n\n📋 Para generar otro permiso use /chuleta"
        )
        return
    lista   = []
    botones = []
    for folio in folios_usuario:
        if folio in timers_activos:
            mins = max(0, 2160 - int(
                (datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60
            ))
            lista.append(f"• {folio} ({mins//60}h {mins%60}min)")
        else:
            lista.append(f"• {folio} (sin timer)")
        botones.append([InlineKeyboardButton(
            text=f"⏹️ Detener {folio}", callback_data=f"detener_{folio}"
        )])
    await message.answer(
        f"📋 FOLIOS JALISCO ACTIVOS ({len(folios_usuario)})\n\n" +
        "\n".join(lista) +
        "\n\n⏰ Timer 36h por folio.\n📸 Envía imagen para comprobante.\n\n"
        "📋 Para generar otro permiso use /chuleta",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones)
    )

@dp.message(lambda m: m.text and any(
    p in m.text.lower() for p in
    ['costo','precio','cuanto','cuánto','deposito','depósito','pago','valor','monto']
))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 El costo del permiso es ${PRECIO_PERMISO}.\n\n📋 Para generar otro permiso use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Jalisco.")

# ============ FASTAPI =========================================================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema Jalisco activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    try:
        await inicializar_folio_cursors()
        _cargar_plantillas()
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            wh = f"{BASE_URL}/webhook"
            await bot.set_webhook(wh, allowed_updates=["message", "callback_query"])
            print(f"[WEBHOOK] Configurado: {wh}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Sin webhook")
        print(f"[SISTEMA] Jalisco v17.1 iniciado — "
              f"PDF417 {'✅' if PDF417_DISPONIBLE else '⚠️ (fallback QR)'}")
        yield
    except Exception as e:
        print(f"[ERROR CRÍTICO] {e}")
        yield
    finally:
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Jalisco Digital", version="17.1")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    return {
        "ok":                True,
        "version":           "17.1 - timeout fix + cache plantillas + QR/PDF417 originales",
        "entidad":           "Jalisco",
        "pdf417_disponible": PDF417_DISPONIBLE,
        "active_timers":     len(timers_activos),
        "cursors_actuales":  _folio_cursors,
        "fixes_v17.1": [
            "AiohttpSession timeout=300s — elimina el HTTP timeout error",
            "Plantillas PDF cargadas en RAM al inicio — sin I/O de disco por permiso",
            "fitz.open(stream=bytes) — sin abrir archivos en cada PDF",
            "QR y PDF417 con qrcode original — sin cambios de librería",
            "guardar_folio_con_reintento — busca siguiente folio disponible automáticamente",
        ]
    }

@app.get("/status")
async def status_detail():
    return {
        "sistema":             "Jalisco Digital v17.1",
        "pdf417_disponible":   PDF417_DISPONIBLE,
        "total_timers":        len(timers_activos),
        "folios_activos":      list(timers_activos.keys()),
        "cursors_por_prefijo": _folio_cursors,
        "timestamp":           datetime.now().isoformat(),
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"[ARRANQUE] Jalisco v17.1 — puerto {port}")
    print(f"[PDF417] {'Disponible ✅' if PDF417_DISPONIBLE else 'No disponible, usando QR fallback'}")
    uvicorn.run(app, host="0.0.0.0", port=port)
