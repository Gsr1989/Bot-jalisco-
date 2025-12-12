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
import fitz
import pytz
import pdf417gen
from PIL import Image
import random
from io import BytesIO
import base64
from pdf417gen import encode, render_image
import qrcode
import string
import csv
import json
import io
import time
import re

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "jalisco1.pdf"
PLANTILLA_BUENO = "jalisco.pdf"

PRECIO_PERMISO = 250
PRECIO_FIJO_PAGINA2 = 1080  # Precio fijo para página 2

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

URL_CONSULTA_BASE = "https://serviciodigital-jaliscogobmx.onrender.com"

coords_qr_dinamico = {
    "x": 950,
    "y": 700,
    "ancho": 150,
    "alto": 150
}

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ============ FOLIOS CONSECUTIVOS CON PREFIJO ============
PREFIJOS_VALIDOS = {
    "1": 900000000,
    "2": 800000000,
    "3": 700000000,
}

_folio_cursors = {}
_folio_lock = asyncio.Lock()

def _leer_cursors_local():
    try:
        with open("folio_cursors.json") as f:
            data = json.load(f)
            return {k: int(v) for k, v in data.items()}
    except Exception:
        return {}

def _guardar_cursors_local(cursors: dict):
    try:
        with open("folio_cursors.json", "w") as f:
            json.dump(cursors, f)
    except Exception as e:
        print(f"[WARN] No se pudo persistir cursors: {e}")

def _leer_ultimo_folio_por_prefijo(prefijo: str):
    try:
        inicio_rango = int(prefijo) * 100000000
        fin_rango = inicio_rango + 100000000
        
        resp = (
            supabase.table("folios_registrados")
            .select("folio")
            .order("folio", desc=True)
            .limit(2000)
            .execute()
        )
        
        max_num = None
        for row in (resp.data or []):
            s = str(row.get("folio", "")).strip()
            if re.fullmatch(r"\d{9}", s):
                val = int(s)
                if inicio_rango <= val < fin_rango:
                    if max_num is None or val > max_num:
                        max_num = val
        
        if max_num is not None:
            print(f"[FOLIO][DB] Último folio prefijo {prefijo}: {max_num}")
            return max_num
        
        print(f"[FOLIO][DB] No hay folios con prefijo {prefijo}, usando base")
        return PREFIJOS_VALIDOS[prefijo] - 1
        
    except Exception as e:
        print(f"[ERROR] Consultando folios prefijo {prefijo}: {e}")
        return PREFIJOS_VALIDOS[prefijo] - 1

async def inicializar_folio_cursors():
    global _folio_cursors
    
    cursors_local = _leer_cursors_local()
    
    for prefijo in PREFIJOS_VALIDOS.keys():
        ultimo_db = _leer_ultimo_folio_por_prefijo(prefijo)
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
        base = PREFIJOS_VALIDOS[prefijo]
        limite = base + 100000000
        
        _folio_cursors[prefijo] += 1
        
        if _folio_cursors[prefijo] >= limite:
            _folio_cursors[prefijo] = base
        
        _guardar_cursors_local(_folio_cursors)
        folio = f"{_folio_cursors[prefijo]:09d}"
        print(f"[FOLIO] Generado prefijo {prefijo}: {folio}")
        return folio

async def guardar_folio_con_reintento(datos, user_id, username, prefijo="1"):
    max_intentos = 10000000
    
    for intento in range(max_intentos):
        if "folio" not in datos or not re.fullmatch(r"\d{9}", str(datos.get("folio", ""))):
            datos["folio"] = await generar_folio_con_prefijo(prefijo)
        
        try:
            supabase.table("folios_registrados").insert({
                "folio": datos["folio"],
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
            
            print(f"[ÉXITO] ✅ Folio {datos['folio']} guardado (intento {intento + 1})")
            return True
            
        except Exception as e:
            em = str(e).lower()
            if "duplicate" in em or "unique constraint" in em or "23505" in em:
                print(f"[DUPLICADO] {datos['folio']} existe, generando siguiente (intento {intento + 1}/{max_intentos})")
                datos["folio"] = None
                await asyncio.sleep(0.1)
                continue
            
            print(f"[ERROR BD] {e}")
            return False
    
    print(f"[ERROR FATAL] No se pudo guardar tras {max_intentos} intentos")
    return False

# ============ SISTEMA DE FOLIOS PÁGINA 2 ============
def _leer_folios_pagina2():
    """Lee los contadores de la página 2"""
    try:
        with open("folios_pagina2.json") as f:
            return json.load(f)
    except Exception:
        return {
            "referencia_pago": 273312001734,
            "num_autorizacion": 370803,
            "folio_seguimiento": "GZUdr61oqv2",
            "linea_captura": 41340816
        }

def _guardar_folios_pagina2(folios: dict):
    """Guarda los contadores de la página 2"""
    try:
        with open("folios_pagina2.json", "w") as f:
            json.dump(folios, f)
    except Exception as e:
        print(f"[WARN] No se pudo persistir folios página 2: {e}")

def _incrementar_alfanumerico(codigo: str) -> str:
    """Incrementa un código alfanumérico tipo GZUdr61oqv2 → GZUdr61oqw1"""
    # Separar parte fija (letras) y parte variable (números+letra final)
    # GZUdr61oqv2 -> "GZUdr" + "61oqv" + "2"
    
    # Buscar dónde empiezan los números
    indice_numeros = 0
    for i, char in enumerate(codigo):
        if char.isdigit():
            indice_numeros = i
            break
    
    parte_fija = codigo[:indice_numeros]  # "GZUdr"
    parte_variable = codigo[indice_numeros:]  # "61oqv2"
    
    # Extraer número y sufijo
    # "61oqv2" -> "61" + "oqv" + "2"
    match = re.match(r'(\d+)([a-z]+)(\d+)', parte_variable)
    if match:
        numero = int(match.group(1))
        sufijo_letras = match.group(2)
        digito_final = int(match.group(3))
        
        # Incrementar dígito final
        digito_final += 1
        
        # Si pasa de 9, incrementar sufijo de letras
        if digito_final > 9:
            digito_final = 0
            # Incrementar sufijo alfabético (oqv -> oqw)
            sufijo_letras = _incrementar_sufijo_alfabetico(sufijo_letras)
        
        nuevo_codigo = f"{parte_fija}{numero}{sufijo_letras}{digito_final}"
        return nuevo_codigo
    
    # Fallback: solo incrementar último dígito
    return codigo[:-1] + str((int(codigo[-1]) + 1) % 10)

def _incrementar_sufijo_alfabetico(sufijo: str) -> str:
    """Incrementa un sufijo alfabético: oqv → oqw → oqx ... → oqz → ora"""
    chars = list(sufijo)
    
    # Empezar desde el final
    for i in range(len(chars) - 1, -1, -1):
        if chars[i] == 'z':
            chars[i] = 'a'
            continue
        else:
            chars[i] = chr(ord(chars[i]) + 1)
            break
    
    return ''.join(chars)

def generar_folios_pagina2() -> dict:
    """Genera nuevos folios para la página 2 e incrementa contadores"""
    folios = _leer_folios_pagina2()
    
    # Incrementar todos los contadores
    folios["referencia_pago"] += 1
    folios["num_autorizacion"] += 1
    folios["folio_seguimiento"] = _incrementar_alfanumerico(folios["folio_seguimiento"])
    folios["linea_captura"] += 1
    
    # Guardar nuevos valores
    _guardar_folios_pagina2(folios)
    
    print(f"[PÁGINA 2] Folios generados: Ref={folios['referencia_pago']}, "
          f"Auth={folios['num_autorizacion']}, Seg={folios['folio_seguimiento']}, "
          f"Linea={folios['linea_captura']}")
    
    return folios

# ------------ TIMER MANAGEMENT - 36 HORAS ------------
timers_activos = {}
user_folios = {}
pending_comprobantes = {}

TOTAL_MINUTOS_TIMER = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - ESTADO DE JALISCO\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"Para iniciar un nuevo trámite use /chuleta"
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
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} (36 horas)")
        
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
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer 36h iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}")

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

# ============ COORDENADAS Y FUNCIONES PDF ============
coords_jalisco = {
    "folio": (995, 390, 14, (0, 0, 0)),
    "marca": (320, 391, 14, (0, 0, 0)),
    "serie": (1005, 392, 14, (0, 0, 0)),
    "linea": (320, 421, 14, (0, 0, 0)),
    "motor": (300, 260, 14, (0, 0, 0)),
    "anio": (320, 451, 14, (0, 0, 0)),
    "color": (320, 481, 14, (0, 0, 0)),
    "nombre": (320, 361, 14, (0, 0, 0)),
    "fecha_exp": (120, 350, 14, (0, 0, 0)),
    "fecha_exp_completa": (120, 370, 14, (0, 0, 0)),
    "fecha_ven": (275, 645, 90, (0, 0, 0))
}

# Coordenadas para página 2 (basadas en la imagen del comprobante)
coords_pagina2 = {
    "referencia_pago": (380, 123, 10, (0, 0, 0)),      # 273312001734
    "num_autorizacion": (380, 140, 10, (0, 0, 0)),     # 370803
    "total_pagado": (380, 170, 10, (0, 0, 0)),         # $1080.00 MN
    "folio_seguimiento": (380, 240, 10, (0, 0, 0)),    # GZUdr61oqv2
    "linea_captura": (380, 270, 10, (0, 0, 0))         # 41340816
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

# ============ GENERACIÓN PDF UNIFICADO (2 PÁGINAS EN 1 ARCHIVO) ============
def generar_pdf_unificado(datos: dict) -> str:
    """Genera UN SOLO PDF con ambas plantillas (2 páginas)"""
    fol = datos["folio"]
    fecha_exp = datos["fecha_exp"]
    fecha_ven = datos["fecha_ven"]
    
    zona_mexico = pytz.timezone("America/Mexico_City")
    _ = datetime.now(zona_mexico)
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_completo.pdf")
    
    try:
        # ===== PROCESAR PRIMERA PÁGINA (jalisco1.pdf) =====
        doc1 = fitz.open(PLANTILLA_PDF)
        pg1 = doc1[0]
        
        for campo in ["marca", "linea", "anio", "serie", "nombre", "color"]:
            if campo in coords_jalisco and campo in datos:
                x, y, s, col = coords_jalisco[campo]
                pg1.insert_text((x, y), datos.get(campo, ""), fontsize=s, color=col)
        
        pg1.insert_text(coords_jalisco["fecha_ven"][:2], fecha_ven.strftime("%d/%m/%Y"),
                       fontsize=coords_jalisco["fecha_ven"][2], color=coords_jalisco["fecha_ven"][3])
        
        # FOLIO SIN ASTERISCOS: Bajado 20 puntos (425 + 20 = 445)
        pg1.insert_text((960, 445), fol, fontsize=14, color=(0, 0, 0))
        
        fecha_actual_str = fecha_exp.strftime("%d/%m/%Y")
        pg1.insert_text((445, 880), fecha_actual_str, fontsize=33, color=(0, 0, 0))
        
        fol_rep = obtener_folio_representativo()
        pg1.insert_text((305, 880), str(fol_rep), fontsize=32, color=(0, 0, 0))
        pg1.insert_text((605, 203), str(fol_rep), fontsize=55, color=(0, 0, 0))
        incrementar_folio_representativo(fol_rep)
        
        pg1.insert_text((920, 695), f"*{fol}*", fontsize=30, color=(0, 0, 0), fontname="Courier")
        pg1.insert_text((950, 870), "VENTANILLA: DIGITAL", fontsize=14, color=(0, 0, 0))
        
        contenido_ine = f"""FOLIO:{fol}
MARCA:{datos.get('marca', '')}
LINEA:{datos.get('linea', '')}
ANIO:{datos.get('anio', '')}
SERIE:{datos.get('serie', '')}
MOTOR:{datos.get('motor', '')}"""
        ine_img_path = os.path.join(OUTPUT_DIR, f"{fol}_inecode.png")
        generar_codigo_ine(contenido_ine, ine_img_path)
        
        # CÓDIGO PDF417: Bajado 75 puntos total (75 + 20 + 25 + 30 = 150, 132 + 75 = 207)
        pg1.insert_image(fitz.Rect(937.65, 150, 1168.955, 207),
                        filename=ine_img_path, keep_proportion=False, overlay=True)
        
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
            pg1.insert_image(
                fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
                pixmap=qr_pix,
                overlay=True
            )
            print(f"[QR JALISCO] Insertado en página 1")
        
        # ===== PROCESAR SEGUNDA PÁGINA (jalisco.pdf) CON FOLIOS CONSECUTIVOS =====
        doc2 = fitz.open(PLANTILLA_BUENO)
        pg2 = doc2[0]
        
        fecha_hora_str = fecha_exp.strftime("%d/%m/%Y %H:%M")
        pg2.insert_text((380, 195), fecha_hora_str, fontsize=10, fontname="helv", color=(0, 0, 0))
        pg2.insert_text((380, 290), datos['serie'], fontsize=10, fontname="helv", color=(0, 0, 0))
        
        # GENERAR FOLIOS CONSECUTIVOS PARA PÁGINA 2
        folios_pag2 = generar_folios_pagina2()
        
        # Insertar folios de página 2
        pg2.insert_text(coords_pagina2["referencia_pago"][:2], str(folios_pag2["referencia_pago"]),
                       fontsize=coords_pagina2["referencia_pago"][2], color=coords_pagina2["referencia_pago"][3])
        
        pg2.insert_text(coords_pagina2["num_autorizacion"][:2], str(folios_pag2["num_autorizacion"]),
                       fontsize=coords_pagina2["num_autorizacion"][2], color=coords_pagina2["num_autorizacion"][3])
        
        pg2.insert_text(coords_pagina2["total_pagado"][:2], f"${PRECIO_FIJO_PAGINA2}.00 MN",
                       fontsize=coords_pagina2["total_pagado"][2], color=coords_pagina2["total_pagado"][3])
        
        pg2.insert_text(coords_pagina2["folio_seguimiento"][:2], folios_pag2["folio_seguimiento"],
                       fontsize=coords_pagina2["folio_seguimiento"][2], color=coords_pagina2["folio_seguimiento"][3])
        
        pg2.insert_text(coords_pagina2["linea_captura"][:2], str(folios_pag2["linea_captura"]),
                       fontsize=coords_pagina2["linea_captura"][2], color=coords_pagina2["linea_captura"][3])
        
        # ===== UNIR AMBAS PÁGINAS EN UN SOLO PDF =====
        doc_final = fitz.open()
        doc_final.insert_pdf(doc1)
        doc_final.insert_pdf(doc2)
        
        doc_final.save(out)
        
        doc_final.close()
        doc1.close()
        doc2.close()
        
        print(f"[PDF UNIFICADO] ✅ Generado exitosamente: {out} (2 páginas)")
        
    except Exception as e:
        print(f"[ERROR] Generando PDF unificado: {e}")
        doc_fallback = fitz.open()
        page = doc_fallback.new_page()
        page.insert_text((50, 50), f"ERROR - Folio: {fol}", fontsize=12)
        doc_fallback.save(out)
        doc_fallback.close()
    
    return out

# ------------ HANDLERS PRINCIPALES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE JALISCO\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer de 36 horas)"

    await message.answer(
        f"🚗 NUEVO PERMISO - ESTADO DE JALISCO\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        f"⏰ Plazo de pago: 36 horas"
        f"{mensaje_folios}\n\n"
        f"Primer paso: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    if not marca or len(marca) < 2:
        await message.answer("⚠️ Proporcione una MARCA válida (mínimo 2 caracteres):")
        return
    await state.update_data(marca=marca)
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    if not linea:
        await message.answer("⚠️ Proporcione la LÍNEA/MODELO:")
        return
    await state.update_data(linea=linea)
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
    serie = message.text.strip().upper()
    if len(serie) < 5 or len(serie) > 25:
        await message.answer("⚠️ Serie inválida (5 a 25 caracteres):")
        return
    await state.update_data(serie=serie)
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    if len(motor) < 5 or len(motor) > 25:
        await message.answer("⚠️ Motor inválido (5 a 25 caracteres):")
        return
    await state.update_data(motor=motor)
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    if not color or len(color) > 20:
        await message.answer("⚠️ Color inválido (máx. 20 caracteres):")
        return
    await state.update_data(color=color)
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()

    if len(nombre) < 5 or len(nombre) > 60 or len(nombre.split()) < 2:
        await message.answer("⚠️ Nombre completo inválido (mínimo nombre y apellido, máx. 60 caracteres):")
        return

    datos["nombre"] = nombre

    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy
    datos["fecha_ven"] = fecha_ven

    try:
        prefijo = "1"
        
        ok = await guardar_folio_con_reintento(datos, message.from_user.id, message.from_user.username, prefijo)
        if not ok:
            await message.answer("❌ No se pudo registrar el folio. Intenta de nuevo con /chuleta")
            await state.clear()
            return

        folio_final = datos["folio"]

        await message.answer(
            f"🔄 Generando documentación...\n"
            f"<b>Folio:</b> {folio_final}\n"
            f"<b>Titular:</b> {nombre}",
            parse_mode="HTML"
        )

        # Generar PDF UNIFICADO (2 páginas en 1 archivo con folios consecutivos)
        pdf_unificado = generar_pdf_unificado(datos)

        await message.answer_document(
            FSInputFile(pdf_unificado),
            caption=f"📋 PERMISO DE CIRCULACIÓN - JALISCO (COMPLETO)\nFolio: {folio_final}\nVigencia: 30 días\n\n✅ Documento con 2 páginas unificadas"
        )

        try:
            supabase.table("borradores_registrados").insert({
                "folio": folio_final,
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

        await iniciar_timer_eliminacion(message.from_user.id, folio_final)

        await message.answer(
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
            "📸 Envía la foto del comprobante para validar.\n"
            "⚠️ Si no pagas en 36 horas, el folio se elimina automáticamente.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await message.answer(f"❌ Error generando documentación: {str(e)}")
        print(f"Error: {e}")
    finally:
        await state.clear()

@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) > 4:
        folio_admin = texto[4:]
        
        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
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

@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes de pago.\n"
                "Para iniciar uno nuevo usa /chuleta"
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
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer("❌ Error procesando el comprobante. Intenta enviar la foto nuevamente.")

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
                f"📄 Folio: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ Folio confirmado: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer("❌ Error procesando el folio especificado. Intenta de nuevo.")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
                "No tienes folios pendientes de pago.\n"
                "Para nuevo permiso use /chuleta"
            )
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_restante = 2160 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, tiempo_restante)
                horas = tiempo_restante // 60
                minutos = tiempo_restante % 60
                lista_folios.append(f"• {folio} ({horas}h {minutos}min restantes)")
            else:
                lista_folios.append(f"• {folio} (sin timer)")
        
        await message.answer(
            f"📋 FOLIOS JALISCO ACTIVOS ({len(folios_usuario)})\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio tiene timer de 36 horas.\n"
            f"📸 Para enviar comprobante, use imagen."
        )
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer("❌ Error consultando expedientes activos.")

@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 INFORMACIÓN DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "Para iniciar su trámite use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Jalisco.")

# ------------ FASTAPI + LIFESPAN ------------
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

        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            webhook_url = f"{BASE_URL}/webhook"
            await bot.set_webhook(webhook_url, allowed_updates=["message"])
            print(f"[WEBHOOK] Configurado: {webhook_url}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Modo sin webhook")
        print("[SISTEMA] ¡Sistema Digital Jalisco iniciado correctamente!")
        print(f"[PREFIJOS] Configurados: {list(PREFIJOS_VALIDOS.keys())}")
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

app = FastAPI(lifespan=lifespan, title="Sistema Jalisco Digital", version="6.0")

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
    return {
        "ok": True,
        "bot": "Jalisco Permisos Sistema",
        "status": "running",
        "version": "6.0 - PDF Unificado + Folios Consecutivos Página 2 + Timer 36h",
        "entidad": "Jalisco",
        "vigencia": "30 días",
        "timer_eliminacion": "36 horas",
        "active_timers": len(timers_activos),
        "prefijos_configurados": list(PREFIJOS_VALIDOS.keys()),
        "cursors_actuales": _folio_cursors,
        "comando_secreto": "/chuleta (invisible)",
        "folios_pagina2": _leer_folios_pagina2(),
        "caracteristicas": [
            "PDF unificado (2 páginas en 1 archivo)",
            "Folios página 1: Consecutivos por prefijo desde DB",
            "Folios página 2: Consecutivos alfanuméricos independientes",
            "Timer 36 horas con avisos 90/60/30/10",
            "Reintentos automáticos ante duplicados",
            "Comando admin: SERO[folio]",
            "Timers independientes por folio"
        ]
    }

@app.get("/status")
async def status_detail():
    return {
        "sistema": "Jalisco Digital v6.0 - Folios Consecutivos Página 2",
        "entidad": "Jalisco",
        "vigencia_dias": 30,
        "tiempo_eliminacion": "36 horas con avisos 90/60/30/10",
        "total_timers_activos": len(timers_activos),
        "folios_con_timer": list(timers_activos.keys()),
        "usuarios_con_folios": len(user_folios),
        "prefijos_disponibles": PREFIJOS_VALIDOS,
        "cursors_por_prefijo": _folio_cursors,
        "folios_pagina2_actuales": _leer_folios_pagina2(),
        "pdf_output": "UN archivo con 2 páginas (jalisco1 + jalisco con folios)",
        "continuidad": "Folios desde último en DB por prefijo; +1 con lock y reintentos",
        "comando_secreto": "/chuleta (invisible)",
        "timestamp": datetime.now().isoformat(),
        "status": "Operacional"
    }

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] Jalisco v6.0 - PDF Unificado + Folios Consecutivos Página 2")
        print(f"[COMANDO SECRETO] /chuleta")
        print(f"[PREFIJOS] {PREFIJOS_VALIDOS}")
        print(f"[PDF OUTPUT] 1 archivo unificado con 2 páginas + folios consecutivos")
        print(f"[FOLIOS PÁG 2] Ref/Auth/Seg/Linea consecutivos independientes")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
