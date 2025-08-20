from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz  # PyMuPDF
import pytz
import pdf417gen
from PIL import Image

# Importaciones adicionales del Flask
from io import BytesIO
import base64
from pdf417gen import encode, render_image
import qrcode
import string
import csv
import json
import io
import time

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "jalisco.pdf"  # PDF principal (el completo)
PLANTILLA_BUENO = "jalisco1.pdf"  # PDF simple (solo fecha y serie)

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ============ COORDENADAS JALISCO ============
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

# ============ FUNCIÓN GENERAR FOLIO JALISCO ============
def generar_folio_jalisco():
    """
    Lee el mayor folio en la entidad Jalisco y suma +1
    """
    registros = supabase.table("borradores_registros").select("folio").eq("entidad","Jalisco").execute().data
    numeros = []
    for r in registros:
        f = r["folio"]
        try:
            numeros.append(int(f))
        except:
            continue
    siguiente = max(numeros) + 1 if numeros else 5008167415
    return str(siguiente)

# ============ FUNCIÓN FOLIO REPRESENTATIVO ============
def obtener_folio_representativo():
    try:
        with open("folio_representativo.txt") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        with open("folio_representativo.txt", "w") as f:
            f.write("331997")
        return 331997

def incrementar_folio_representativo(folio_actual):
    nuevo = folio_actual + 1
    with open("folio_representativo.txt", "w") as f:
        f.write(str(nuevo))
    return nuevo

# ============ FUNCIÓN GENERAR CÓDIGO INE (PDF417) ============
def generar_codigo_ine(contenido, ruta_salida):
    """Genera los datos PDF417 (como INE)"""
    codes = pdf417gen.encode(contenido, columns=6, security_level=5)
    image = pdf417gen.render_image(codes)  # Devuelve un objeto PIL.Image
    image.save(ruta_salida)

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
    
    # === FECHA Y HORA ACTUAL DE CDMX ===
    zona_cdmx = pytz.timezone("America/Mexico_City")
    ahora_cdmx = datetime.now(zona_cdmx)
    
    # Crear carpeta de salida
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_jalisco.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    # --- Insertar campos normales del formulario ---
    for campo in ["marca", "linea", "anio", "serie", "nombre", "color"]:
        if campo in coords_jalisco and campo in datos:
            x, y, s, col = coords_jalisco[campo]
            pg.insert_text((x, y), datos.get(campo, ""), fontsize=s, color=col)

    # --- Insertar fecha de vencimiento ---
    pg.insert_text(coords_jalisco["fecha_ven"][:2], fecha_ven.strftime("%d/%m/%Y"), 
                   fontsize=coords_jalisco["fecha_ven"][2], color=coords_jalisco["fecha_ven"][3])

    # --- Imprimir FOLIO generado automáticamente ---
    pg.insert_text((930, 391), fol, fontsize=14, color=(0, 0, 0))

    # --- Imprimir FECHA/HORA ACTUAL de emisión ---
    fecha_actual_str = fecha_exp.strftime("%d/%m/%Y")
    pg.insert_text((478, 804), fecha_actual_str, fontsize=32, color=(0, 0, 0))

    # --- Imprimir FOLIO REPRESENTATIVO dos veces ---
    fol_representativo = int(obtener_folio_representativo())
    pg.insert_text((337, 804), str(fol_representativo), fontsize=32, color=(0, 0, 0))
    pg.insert_text((650, 204), str(fol_representativo), fontsize=45, color=(0, 0, 0))
    incrementar_folio_representativo(fol_representativo)

    # --- Imprimir FOLIO con asteriscos al estilo etiqueta ---
    pg.insert_text((910, 620), f"*{fol}*", fontsize=30, color=(0, 0, 0), fontname="Courier")
    pg.insert_text((1083, 800), "DIGITAL", fontsize=14, color=(0, 0, 0)) 
    
    # --- Generar imagen tipo INE y colocarla ---
    contenido_ine = f"""FOLIO:{fol}
MARCA:{datos.get('marca', '')}
LINEA:{datos.get('linea', '')}
ANIO:{datos.get('anio', '')}
SERIE:{datos.get('serie', '')}
MOTOR:{datos.get('motor', '')}"""
    
    ine_img_path = os.path.join(OUTPUT_DIR, f"{fol}_inecode.png")
    generar_codigo_ine(contenido_ine, ine_img_path)

    # --- Insertar imagen en tamaño FIJO (siempre igual) ---
    pg.insert_image(fitz.Rect(937.65, 75, 1168.955, 132), filename=ine_img_path, 
                    keep_proportion=False, overlay=True)

    doc.save(out)
    doc.close()
    
    return out

# ============ PDF BUENO (SIMPLE - SOLO FECHA Y SERIE) ============
def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    """Genera el PDF simple con solo fecha y serie"""
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    
    # Solo insertar serie y fecha como en el código original
    page.insert_text((135.02, 193.88), serie, fontsize=6)
    page.insert_text((190, 324), fecha.strftime("%d/%m/%Y"), fontsize=6)
    
    filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
    doc.save(filename)
    doc.close()
    
    return filename

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Bienvenido al Bot de Permisos Jalisco. Usa /permiso para iniciar")

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer("🚗 Marca del vehículo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip())
    await message.answer("📱 Línea del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip())
    await message.answer("📅 Año del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("❌ Por favor ingresa un año válido (4 dígitos):")
        return
    
    await state.update_data(anio=anio)
    await message.answer("🔢 Número de serie:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip())
    await message.answer("🔧 Número de motor:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip())
    await message.answer("🎨 Color del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip())
    await message.answer("👤 Nombre completo del solicitante:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip()
    
    # Generar folio único de Jalisco
    datos["folio"] = generar_folio_jalisco()

    # -------- FECHAS FORMATOS --------
    hoy = datetime.now()
    vigencia_dias = 30  # Por defecto 30 días
    fecha_ven = hoy + timedelta(days=vigencia_dias)
    
    datos["fecha_exp"] = hoy
    datos["fecha_ven"] = fecha_ven
    
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    # ---------------------------------

    try:
        await message.answer("📄 Generando permisos...")
        
        # Generar ambos PDFs
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

        # Enviar PDF principal
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 Permiso Principal - Folio: {datos['folio']}\n🌟 Jalisco Digital"
        )
        
        # Enviar PDF simple
        await message.answer_document(
            FSInputFile(p2),
            caption=f"✅ EL BUENO - Serie: {datos['serie']}\n📅 Fecha: {hoy.strftime('%d/%m/%Y')}"
        )

        # Guardar en Supabase
        supabase.table("borradores_registros").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "color": datos["color"],
            "contribuyente": datos["nombre"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": "Jalisco",
        }).execute()

        await message.answer(
            f"🎉 ¡Permisos generados exitosamente!\n\n"
            f"📋 **Resumen:**\n"
            f"🆔 Folio: {datos['folio']}\n"
            f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"📅 Vigencia: {datos['vigencia']}\n"
            f"👤 Solicitante: {datos['nombre']}\n\n"
            f"✅ Registro guardado correctamente\n"
            f"🔄 Usa /permiso para generar otro"
        )
        
    except Exception as e:
        await message.answer(f"❌ Error al generar permisos: {e}")
        print(f"Error: {e}")
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    await message.answer(
        "👋 ¡Hola! Soy el Bot de Permisos de Jalisco.\n\n"
        "🚗 Usa /permiso para generar tu permiso de circulación\n"
        "📄 Genero 2 documentos: Principal y EL BUENO\n"
        "⚡ Proceso rápido y seguro"
    )

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    return {"ok": True, "bot": "Jalisco Permisos", "status": "running"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}
