from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
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
    print("[PDF417] Librer\u00eda disponible \u2705")
except ImportError:
    PDF417_DISPONIBLE = False
    print("[PDF417] Librer\u00eda NO disponible, usando QR fallback")

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

# Rect\u00e1ngulo donde va el PDF417 (donde estaba el PDF417 original)
RECT_PDF417 = fitz.Rect(932.65, 807, 1141.395, 852.127)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ============ FOLIOS CONSECUTIVOS ============
PREFIJOS_VALIDOS = {
    "1": 900001500,
    "2": 800000000,
    "3": 700000000,
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
    """S\u00edncrono \u2014 usar con asyncio.to_thread."""
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
            print(f"[FOLIO][DB] \u00daltimo folio prefijo {prefijo}: {ultimo}")
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
    """S\u00edncrono \u2014 usar con asyncio.to_thread."""
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
    """S\u00edncrono \u2014 usar con asyncio.to_thread."""
    hoy      = datos["fecha_exp"]
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
    for intento in range(10_000_000):
        if "folio" not in datos or not re.fullmatch(r"\d{9}", str(datos.get("folio", ""))):
            datos["folio"] = await generar_folio_con_prefijo(prefijo)
        try:
            await asyncio.to_thread(_sb_insertar_folio, datos, user_id, username)
            print(f"[\u00c9XITO] \u2705 Folio {datos['folio']} guardado (intento {intento+1})")
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

# ============ FOLIOS P\u00c1GINA 2 ============
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
        print(f"[WARN] No se pudo persistir folios p\u00e1gina 2: {e}")

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
    print(f"[P\u00c1GINA 2] Ref={folios['referencia_pago']}, Auth={folios['num_autorizacion']}, "
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
                f"\u23f0 TIEMPO AGOTADO - ESTADO DE JALISCO\n\n"
                f"El folio {folio} ha sido eliminado por no completar el pago en 36 horas.\n\n"
                f"\ud83d\udccb Para generar otro permiso use /chuleta"
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
            f"\u26a1 RECORDATORIO DE PAGO - JALISCO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"\ud83d\udcf8 Env\u00ede su comprobante de pago (imagen).\n\n"
            f"\ud83d\udccb Para generar otro permiso use /chuleta"
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
    "marca":   (340, 332, 14, (0,0,0)),
    "serie":   (920, 332, 14, (0,0,0)),
    "linea":   (340, 360, 14, (0,0,0)),
    "anio":    (340, 389, 14, (0,0,0)),
    "color":   (340, 418, 14, (0,0,0)),
    "nombre":  (340, 304, 14, (0,0,0)),
    "fecha_ven": (285, 570, 90, (0,0,0)),
}

coords_pagina2 = {
    "referencia_pago":   (380, 123, 10, (0,0,0)),
    "num_autorizacion":  (380, 147, 10, (0,0,0)),
    "total_pagado":      (380, 170, 10, (0,0,0)),
    "folio_seguimiento": (380, 243, 10, (0,0,0)),
    "linea_captura":     (380, 265, 10, (0,0,0)),
}

# ============ QR PRINCIPAL (sin cambios) ============
def _generar_qr_jalisco(folio: str):
    """S\u00edncrono \u2014 usar con asyncio.to_thread."""
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

# ============ PDF417 RECTANGULAR (reemplaza Aztec/PDF417 anterior) ============
def _generar_pdf417(datos: dict) -> Image.Image | None:
    """
    Genera un PDF417 con todos los datos del veh\u00edculo separados por espacio.
    S\u00edncrono \u2014 usar con asyncio.to_thread.
    """
    # Texto que se leer\u00e1 al escanear
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

    # Dimensiones del rect\u00e1ngulo destino en puntos PDF
    ancho_pts = int(RECT_PDF417.x1 - RECT_PDF417.x0)  # \u2248 209
    alto_pts  = int(RECT_PDF417.y1 - RECT_PDF417.y0)   # \u2248 45

    if PDF417_DISPONIBLE:
        try:
            # columns=8 genera un c\u00f3digo horizontal/ancho
            codes = pdf417_encode(texto, columns=8, security_level=2)
            img   = pdf417_render(codes, scale=2, ratio=2)
            img   = img.convert("RGB")
            img   = img.resize((ancho_pts * 4, alto_pts * 4), Image.LANCZOS)
            print(f"[PDF417] Generado: {texto[:60]}...")
            return img
        except Exception as e:
            print(f"[ERROR PDF417] {e} \u2014 usando QR fallback")

    # Fallback: QR estirado con los datos del veh\u00edculo
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

# ============ GENERACI\u00d3N PDF (s\u00edncrono, llamar con to_thread) ================
def _generar_pdf_unificado(datos: dict) -> str:
    fol       = datos["folio"]
    fecha_exp = datos["fecha_exp"]
    fecha_ven = datos["fecha_ven"]

    zona_mexico = pytz.timezone("America/Mexico_City")
    ahora_cdmx  = datetime.now(zona_mexico)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_completo.pdf")

    try:
        doc1 = fitz.open(PLANTILLA_PDF)
        pg1  = doc1[0]

        # \u2500\u2500 Datos del veh\u00edculo \u2500\u2500
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

        fol_rep     = obtener_folio_representativo()
        folio_grande = f"4A-DVM/{fol_rep}"
        pg1.insert_text((240, 830), folio_grande, fontsize=32, color=(0,0,0), fontname="hebo")
        pg1.insert_text((480, 182), folio_grande, fontsize=63, color=(0,0,0), fontname="hebo")

        folio_chico = f"DVM-{fol_rep}   {ahora_cdmx.strftime('%d/%m/%Y')}  {ahora_cdmx.strftime('%H:%M:%S')}"
        pg1.insert_text((915, 760), folio_chico, fontsize=14, color=(0,0,0), fontname="hebo")

        incrementar_folio_representativo(fol_rep)

        pg1.insert_text((935, 600), f"*{fol}*", fontsize=30, color=(0,0,0), fontname="Courier")
        pg1.insert_text((915, 775), "EXPEDICION: VENTANILLA 32", fontsize=12, color=(0,0,0), fontname="hebo")

        # \u2500\u2500 QR cuadrado (posici\u00f3n original, sin cambios) \u2500\u2500
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
            print("[QR] Insertado en posici\u00f3n original")

        # \u2500\u2500 PDF417 rectangular con datos del veh\u00edculo \u2500\u2500
        img_pdf417 = _generar_pdf417(datos)
        if img_pdf417:
            buf2 = BytesIO()
            img_pdf417.save(buf2, format="PNG")
            buf2.seek(0)
            pg1.insert_image(RECT_PDF417, pixmap=fitz.Pixmap(buf2.read()), overlay=True)
            print("[PDF417] Insertado en rect rectangular")

        # \u2500\u2500 P\u00e1gina 2 \u2500\u2500
        doc2 = fitz.open(PLANTILLA_BUENO)
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

        print(f"[PDF UNIFICADO] \u2705 Generado: {out}")

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

        # \u2500\u2500 PDF en hilo separado \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        pdf_path = await asyncio.to_thread(_generar_pdf_unificado, datos)

        folio_final = datos["folio"]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="\ud83d\udd11 Validar Admin", callback_data=f"validar_{folio_final}"),
            InlineKeyboardButton(text="\u23f9\ufe0f Detener Timer", callback_data=f"detener_{folio_final}")
        ]])

        await bot.send_document(
            chat_id,
            FSInputFile(pdf_path),
            caption=(
                f"\ud83d\udccb PERMISO DE CIRCULACI\u00d3N - JALISCO\n"
                f"Folio: {folio_final}\nVigencia: 30 d\u00edas ({fecha_ven.strftime('%d/%m/%Y')})\n\n"
                f"\u2705 Documento con 2 p\u00e1ginas unificadas\n\u23f0 TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        # \u2500\u2500 Borrador en Supabase en hilo separado \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        try:
            await asyncio.to_thread(_sb_insertar_borrador, datos, user_id)
        except Exception as e:
            print(f"[WARN] Error guardando borradores: {e}")

        await iniciar_timer_eliminacion(user_id, folio_final)

        await bot.send_message(
            user_id,
            "\ud83d\udcb0 INSTRUCCIONES DE PAGO\n\n"
            f"\ud83d\udcc4 Folio: {folio_final}\n"
            f"\ud83d\udcb5 Monto: ${PRECIO_PERMISO}\n"
            "\u23f0 Tiempo l\u00edmite: 36 horas\n\n"
            "\ud83c\udfe6 TRANSFERENCIA:\n"
            "\u2022 Instituci\u00f3n: SPIN BY OXXO\n"
            "\u2022 Titular: GUILLERMO S.R\n"
            "\u2022 Cuenta: 728969000048442454\n"
            f"\u2022 Concepto: Permiso {folio_final}\n\n"
            "\ud83c\udfea OXXO:\n"
            "\u2022 Referencia: 2242170180214090\n"
            "\u2022 Titular: GUILLERMO S.R\n\n"
            "\ud83d\udcf8 Env\u00eda foto del comprobante para validar.\n"
            "\u26a0\ufe0f Sin pago en 36h el folio se elimina.\n\n"
            "\ud83d\udccb Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        print(f"[ERROR] _generar_y_enviar_background folio {datos.get('folio','?')}: {e}")
        try:
            await bot.send_message(
                user_id,
                f"\u274c Error al generar el documento: {e}\n\nUse /chuleta para reintentar."
            )
        except Exception:
            pass

# ============ HANDLERS BOT ====================================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "\ud83c\udfdb\ufe0f SISTEMA DIGITAL DEL ESTADO DE JALISCO\n\n"
        f"\ud83d\udcb0 Costo: ${PRECIO_PERMISO}\n"
        "\u23f0 Tiempo l\u00edmite: 36 horas\n\n"
        "\u26a0\ufe0f Su folio ser\u00e1 eliminado si no paga dentro del tiempo l\u00edmite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    folios_activos = obtener_folios_usuario(message.from_user.id)

    # \u2500\u2500 Mostrar folios activos con bot\u00f3n individual de Detener Timer \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    if folios_activos:
        lineas = []
        for f in folios_activos:
            if f in timers_activos:
                mins = max(0, 2160 - int(
                    (datetime.now() - timers_activos[f]["start_time"]).total_seconds() / 60
                ))
                lineas.append(f"\u2022 {f}  ({mins//60}h {mins%60}min restantes)")
            else:
                lineas.append(f"\u2022 {f}  (sin timer)")

        # Un bot\u00f3n "Detener" por folio activo
        botones = [
            [InlineKeyboardButton(text=f"\u23f9\ufe0f Detener {f}", callback_data=f"detener_{f}")]
            for f in folios_activos
        ]
        kb_folios = InlineKeyboardMarkup(inline_keyboard=botones)

        await message.answer(
            f"\ud83d\udccb FOLIOS JALISCO ACTIVOS ({len(folios_activos)}):\n\n" +
            "\n".join(lineas) +
            "\n\nPuedes detener el timer de cualquier folio:",
            reply_markup=kb_folios
        )

    await message.answer(
        f"\ud83d\ude97 NUEVO PERMISO - ESTADO DE JALISCO\n\n"
        f"\ud83d\udcb0 Costo: ${PRECIO_PERMISO}\n"
        f"\u23f0 Plazo de pago: 36 horas\n\n"
        f"Primer paso: MARCA del veh\u00edculo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("L\u00cdNEA/MODELO del veh\u00edculo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("A\u00d1O del veh\u00edculo (4 d\u00edgitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("\u26a0\ufe0f Formato inv\u00e1lido. Use 4 d\u00edgitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("N\u00daMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("N\u00daMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("COLOR del veh\u00edculo:")
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

    # \u2500\u2500 Guardar folio en Supabase (async, con reintento por duplicado) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    ok = await guardar_folio_con_reintento(datos, message.from_user.id,
                                           message.from_user.username, "1")
    if not ok:
        await message.answer(
            "\u274c No se pudo registrar el folio. Intenta de nuevo con /chuleta\n\n"
            "\ud83d\udccb Para generar otro permiso use /chuleta"
        )
        return

    folio_final = datos["folio"]

    await message.answer(
        f"\ud83d\udd04 Generando documentaci\u00f3n...\n"
        f"<b>Folio:</b> {folio_final}\n"
        f"<b>Titular:</b> {datos['nombre']}",
        parse_mode="HTML"
    )

    # \u2500\u2500 PDF en background \u2014 no bloquea el webhook \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
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
                    {"estado": "VALIDADO_ADMIN", "fecha_comp
