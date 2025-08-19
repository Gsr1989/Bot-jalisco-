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

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "jalisco1.pdf"
PLANTILLA_BUENO = "jalisco.pdf"

# Coordenadas Jalisco
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
    "fecha_exp": (120, 350, 14, (0, 0, 0)),              # Solo fecha
    "fecha_exp_completa": (120, 370, 14, (0, 0, 0)),     # Fecha con hora
    "fecha_ven": (310, 605, 90, (0, 0, 0))               # Vencimiento gigante
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ FOLIO FUNCTIONS ------------
def generar_folio_jalisco():
    """
    Lee el mayor folio en la entidad Jalisco y suma +1
    """
    try:
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
    except:
        return "5008167415"

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

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ------------ PDF FUNCTIONS ------------
def generar_pdf_principal(datos: dict) -> str:
    doc = fitz.open(PLANTILLA_PDF)
    page = doc[0]

    # Usar coordenadas de Jalisco
    page.insert_text(coords_jalisco["folio"][:2], datos["folio"], fontsize=coords_jalisco["folio"][2], color=coords_jalisco["folio"][3])
    page.insert_text(coords_jalisco["marca"][:2], datos["marca"], fontsize=coords_jalisco["marca"][2], color=coords_jalisco["marca"][3])
    page.insert_text(coords_jalisco["serie"][:2], datos["serie"], fontsize=coords_jalisco["serie"][2], color=coords_jalisco["serie"][3])
    page.insert_text(coords_jalisco["linea"][:2], datos["linea"], fontsize=coords_jalisco["linea"][2], color=coords_jalisco["linea"][3])
    page.insert_text(coords_jalisco["motor"][:2], datos["motor"], fontsize=coords_jalisco["motor"][2], color=coords_jalisco["motor"][3])
    page.insert_text(coords_jalisco["anio"][:2], datos["anio"], fontsize=coords_jalisco["anio"][2], color=coords_jalisco["anio"][3])
    page.insert_text(coords_jalisco["color"][:2], datos["color"], fontsize=coords_jalisco["color"][2], color=coords_jalisco["color"][3])
    page.insert_text(coords_jalisco["nombre"][:2], datos["nombre"], fontsize=coords_jalisco["nombre"][2], color=coords_jalisco["nombre"][3])
    
    # Fechas
    page.insert_text(coords_jalisco["fecha_exp"][:2], datos["fecha_exp"], fontsize=coords_jalisco["fecha_exp"][2], color=coords_jalisco["fecha_exp"][3])
    page.insert_text(coords_jalisco["fecha_exp_completa"][:2], datos["fecha_exp_completa"], fontsize=coords_jalisco["fecha_exp_completa"][2], color=coords_jalisco["fecha_exp_completa"][3])
    page.insert_text(coords_jalisco["fecha_ven"][:2], datos["fecha_ven"], fontsize=coords_jalisco["fecha_ven"][2], color=coords_jalisco["fecha_ven"][3])

    filename = f"{OUTPUT_DIR}/{datos['folio']}_jalisco.pdf"
    doc.save(filename)
    doc.close()
    return filename

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    
    # Usar coordenadas de Jalisco para la segunda plantilla
    fecha_hora_str = fecha.strftime('%d/%m/%Y %H:%M')
    page.insert_text((380, 195), fecha_hora_str, fontsize=10, fontname="helv", color=(0, 0, 0))
    page.insert_text((380, 290), serie, fontsize=10, fontname="helv", color=(0, 0, 0))
    
    filename = f"{OUTPUT_DIR}/{folio}_jalisco2.pdf"
    doc.save(filename)
    doc.close()
    return filename

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Bienvenido al sistema de permisos de Jalisco. Usa /permiso para iniciar")

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer("Marca del vehículo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip())
    await message.answer("Línea:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip())
    await message.answer("Año:")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    await state.update_data(anio=message.text.strip())
    await message.answer("Serie:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip())
    await message.answer("Motor:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip())
    await message.answer("Color del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip())
    await message.answer("Nombre del solicitante:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip()
    datos["folio"] = generar_folio_jalisco()

    # Obtener folio representativo
    folio_rep_actual = obtener_folio_representativo()
    folio_rep_nuevo = incrementar_folio_representativo(folio_rep_actual)

    # -------- FECHAS FORMATOS --------
    hoy = datetime.now()
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")  # Solo fecha
    datos["fecha_exp_completa"] = hoy.strftime("%d/%m/%Y %H:%M:%S")  # Fecha con hora
    
    fecha_ven = hoy + timedelta(days=30)
    datos["fecha_ven"] = fecha_ven.strftime("%d/%m/%Y")  # Vencimiento
    # ---------------------------------

    try:
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 Permiso Jalisco - Folio: {datos['folio']} | Rep: {folio_rep_nuevo}"
        )
        await message.answer_document(
            FSInputFile(p2),
            caption=f"✅ Jalisco 2da Plantilla - Serie: {datos['serie']}"
        )

        # Guardar en base de datos
        supabase.table("borradores_registros").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "color": datos["color"],
            "nombre": datos["nombre"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": "Jalisco",
        }).execute()

        await message.answer("✅ Permiso de Jalisco guardado y registrado correctamente.")
    except Exception as e:
        await message.answer(f"❌ Error al generar: {e}")
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    await message.answer("Usa /permiso para iniciar.")

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

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}
