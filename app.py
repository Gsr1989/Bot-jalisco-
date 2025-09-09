from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta, date
from supabase import create_client, Client
from contextlib import asynccontextmanager, suppress
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
import asyncio
import os
import random
import re
import fitz  # PyMuPDF
from PIL import Image
import qrcode
from io import BytesIO

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "edomex_plantilla_alta_res.pdf"   # PDF principal
PLANTILLA_FLASK = "labuena3.0.pdf"                # PDF simple
ENTIDAD = "edomex"

# Folios EDOMEX (texto, empiezan por 98)
FOLIO_PREFIX = "98"
FOLIO_START = 98000001  # primer folio si no hay registros previos

# Precio del permiso
PRECIO_PERMISO = 180

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== SUPABASE =====================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===================== BOT (AIROGRAM) =====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== TIMERS POR FOLIO =====================
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [folios]}

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        if user_id:
            await bot.send_message(
                user_id,
                "⏰ TIEMPO AGOTADO\n\n"
                f"El folio {folio} fue eliminado por falta de pago.\n"
                "Para tramitar uno nuevo use /permiso"
            )
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"[ERROR] eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(
            user_id,
            "⚡ RECORDATORIO DE PAGO EDOMEX\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO} MXN\n\n"
            "📸 Envíe la foto del comprobante para validar."
        )
    except Exception as e:
        print(f"[ERROR] recordatorio {folio}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para {folio} (user {user_id})")
        for sleep_min, restante in [(30, 90), (30, 60), (30, 30), (20, 10)]:
            await asyncio.sleep(sleep_min * 60)
            if folio not in timers_activos:
                return
            await enviar_recordatorio(folio, restante)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer on {folio} (activos={len(timers_activos)})")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        try:
            timers_activos[folio]["task"].cancel()
        except Exception:
            pass
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        print(f"[SISTEMA] Timer cancelado {folio}")

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

# ===================== COORDENADAS PDF =====================
coords_edomex = {
    "folio": (535, 135, 14, (1, 0, 0)),
    "marca": (109, 190, 10, (0, 0, 0)),
    "serie": (230, 233, 10, (0, 0, 0)),
    "linea": (238, 190, 10, (0, 0, 0)),
    "motor": (104, 233, 10, (0, 0, 0)),
    "anio":  (410, 190, 10, (0, 0, 0)),
    "color": (400, 233, 10, (0, 0, 0)),
    "fecha_exp": (190, 280, 10, (0, 0, 0)),
    "fecha_ven": (380, 280, 10, (0, 0, 0)),
    "nombre": (394, 320, 10, (0, 0, 0)),
}

# ===================== FOLIOS EDOMEX =====================
def _max_folio_edomex() -> int | None:
    try:
        resp = (
            supabase.table("folios_registrados")
            .select("folio")
            .eq("entidad", ENTIDAD)
            .order("folio", desc=True)
            .limit(1000)
            .execute()
        )
        max_val = None
        for row in (resp.data or []):
            s = str(row.get("folio", "")).strip()
            if re.fullmatch(rf"{FOLIO_PREFIX}\d+", s):
                v = int(s)
                if max_val is None or v > max_val:
                    max_val = v
        return max_val
    except Exception as e:
        print(f"[ERROR] leyendo max folio EDOMEX: {e}")
        return None

def generar_folio_edomex() -> str:
    max_prev = _max_folio_edomex()
    next_val = (max_prev + 1) if max_prev else FOLIO_START
    return str(next_val)

# ===================== QR =====================
URL_CONSULTA_BASE = "https://sfpyaedomexicoconsultapermisodigital.onrender.com"

def generar_qr_dinamico_edomex(folio: str):
    try:
        url_directa = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url_directa)
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR] {url_directa}")
        return img_qr, url_directa
    except Exception as e:
        print(f"[ERROR] QR: {e}")
        return None, None

# ===================== PDFs =====================
def generar_pdf_flask(fecha_expedicion: datetime, numero_serie: str, folio: str) -> str | None:
    try:
        ruta_pdf = f"{OUTPUT_DIR}/{folio}_simple.pdf"
        doc = fitz.open(PLANTILLA_FLASK)
        page = doc[0]
        page.insert_text((80, 142), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0, 0, 0))
        page.insert_text((218, 142), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0, 0, 0))
        page.insert_text((182, 283), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=9, fontname="helv", color=(0, 0, 0))
        page.insert_text((130, 435), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=20, fontname="helv", color=(0, 0, 0))
        page.insert_text((162, 185), numero_serie, fontsize=9, fontname="helv", color=(0, 0, 0))
        doc.save(ruta_pdf); doc.close()
        return ruta_pdf
    except Exception as e:
        print(f"[ERROR] PDF Flask: {e}")
        return None

def generar_pdf_principal(datos: dict) -> str:
    fol = datos["folio"]
    out = os.path.join(OUTPUT_DIR, f"{fol}_edomex.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    # Folio
    x, y, s, col = coords_edomex["folio"]
    pg.insert_text((x, y), fol, fontsize=s, color=col)

    # Fechas
    x, y, s, col = coords_edomex["fecha_exp"]
    pg.insert_text((x, y), datos["fecha_exp"], fontsize=s, color=col)
    x, y, s, col = coords_edomex["fecha_ven"]
    pg.insert_text((x, y), datos["fecha_ven"], fontsize=s, color=col)

    # Vehículo
    for campo in ["marca", "serie", "linea", "motor", "anio", "color"]:
        if campo in coords_edomex:
            x, y, s, col = coords_edomex[campo]
            pg.insert_text((x, y), str(datos.get(campo, "")), fontsize=s, color=col)

    # Nombre
    x, y, s, col = coords_edomex["nombre"]
    pg.insert_text((x, y), datos.get("nombre", ""), fontsize=s, color=col)

    # QR
    img_qr, _ = generar_qr_dinamico_edomex(fol)
    if img_qr:
        buf = BytesIO(); img_qr.save(buf, format="PNG"); buf.seek(0)
        qr_pix = fitz.Pixmap(buf.read())
        pg.insert_image(fitz.Rect(80, 370, 80 + 82, 370 + 82), pixmap=qr_pix, overlay=True)

    doc.save(out); doc.close()
    return out

# ===================== FSM =====================
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ===================== HANDLERS =====================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ Sistema Digital de Permisos EDOMEX\n"
        f"💰 Costo del permiso: ${PRECIO_PERMISO} MXN\n"
        "⏰ Tiempo límite para pago: 2 horas\n"
        "📸 Pago por transferencia y OXXO\n\n"
        "Use /permiso para iniciar. Su folio se elimina si no paga a tiempo."
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    msg = ""
    if folios_activos:
        msg = "\n\n📋 FOLIOS ACTIVOS: " + ", ".join(folios_activos)
    await message.answer(
        "🚗 TRÁMITE DE PERMISO EDOMEX\n"
        f"Costo: ${PRECIO_PERMISO} MXN | Límite: 2 h\n"
        "Concepto de pago: su folio asignado."
        + msg +
        "\n\n**Paso 1/7:** Indique la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(f"✅ MARCA: {marca}\n\n**Paso 2/7:** LÍNEA/MODELO:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(f"✅ LÍNEA: {linea}\n\n**Paso 3/7:** AÑO (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ El año debe ser de 4 dígitos (ej. 2021). Intente de nuevo:")
        return
    await state.update_data(anio=anio)
    await message.answer(f"✅ AÑO: {anio}\n\n**Paso 4/7:** NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer("⚠️ El número de serie parece incompleto. Intente nuevamente:")
        return
    await state.update_data(serie=serie)
    await message.answer(f"✅ SERIE: {serie}\n\n**Paso 5/7:** NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(f"✅ MOTOR: {motor}\n\n**Paso 6/7:** COLOR:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(f"✅ COLOR: {color}\n\n**Paso 7/7:** NOMBRE COMPLETO del titular:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre

    # Fechas
    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = fecha_ven.strftime("%d/%m/%Y")

    # FOLIO definitivo
    folio_def = generar_folio_edomex()
    datos["folio"] = folio_def

    await message.answer(
        "🔄 PROCESANDO PERMISO EDOMEX...\n\n"
        f"📄 Folio: {folio_def}\n"
        f"👤 Titular: {nombre}\n"
        "Generando documentos..."
    )

    try:
        # PDFs
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_flask(hoy, datos["serie"], folio_def)

        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 PERMISO COMPLETO EDOMEX\nFolio: {folio_def}\nVigencia: 30 días\nQR dinámico incluido."
        )
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"📋 DOCUMENTO COMPLEMENTARIO\nSerie: {datos['serie']}"
            )

        # Guardar en BD
        supabase.table("folios_registrados").insert({
            "folio": folio_def,
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": ENTIDAD,
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username",
        }).execute()

        supabase.table("borradores_registros").insert({
            "folio": folio_def,
            "entidad": ENTIDAD.upper(),
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "fecha_expedicion": hoy.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "contribuyente": datos["nombre"],
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
        }).execute()

        # Timer
        await iniciar_timer_pago(message.from_user.id, folio_def)

        # Instrucciones de pago
        await message.answer(
            "💰 INSTRUCCIONES DE PAGO\n\n"
            f"Folio: {folio_def}\n"
            f"Monto: ${PRECIO_PERMISO} MXN\n"
            "⏰ Límite: 2 horas\n\n"
            "🏦 TRANSFERENCIA\n"
            "• Banco: AZTECA\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cuenta: 127180013037579543\n"
            f"• Concepto: Permiso {folio_def}\n\n"
            "🏪 OXXO\n"
            "• Referencia: 2242170180385581\n"
            "• Tarjeta SPIN – Titular: LIZBETH LAZCANO MOSCO\n\n"
            "📸 Envíe la fotografía del comprobante aquí."
        )
    except Exception as e:
        await message.answer(f"❌ Error generando/registrando el permiso: {e}")
    finally:
        await state.clear()

# ===== Código admin (SERO<folio>) =====
@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) <= 4:
        await message.answer("Formato: SERO<número de folio>  (p.ej. SERO98000001)")
        return
    folio_admin = texto[4:]
    if not re.fullmatch(rf"{FOLIO_PREFIX}\d+", folio_admin):
        await message.answer(f"Folio inválido. Debe comenzar con {FOLIO_PREFIX}.")
        return

    if folio_admin in timers_activos:
        user_con_folio = timers_activos[folio_admin]["user_id"]
        cancelar_timer_folio(folio_admin)
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio_admin).execute()
        supabase.table("borradores_registros").update({
            "estado": "VALIDADO_ADMIN",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio_admin).execute()
        await message.answer(
            f"✅ VALIDADO_ADMIN para {folio_admin}. Timer cancelado. Usuario: {user_con_folio}"
        )
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO\nFolio: {folio_admin}\nSu permiso ya está activo."
            )
        except Exception as e:
            print(f"[WARN] No se pudo notificar al usuario {user_con_folio}: {e}")
    else:
        await message.answer("❌ No hay timer activo para ese folio.")

# ===== Comprobante (foto) =====
@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    if not folios_usuario:
        await message.answer("ℹ️ No tienes permisos pendientes. Usa /permiso para crear uno.")
        return
    if len(folios_usuario) > 1:
        lista = "\n".join(f"• {f}" for f in folios_usuario)
        await message.answer(
            "📄 Tienes varios folios activos.\n"
            f"{lista}\n\nResponde con el NÚMERO DE FOLIO al que corresponde este comprobante."
        )
        return
    folio = folios_usuario[0]
    cancelar_timer_folio(folio)
    supabase.table("folios_registrados").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    supabase.table("borradores_registros").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    await message.answer(
        "✅ Comprobante recibido.\n"
        f"Folio: {folio}\n"
        "El equipo validará el pago y te notificaremos."
    )

# ===== Folios activos =====
@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    if not folios_usuario:
        await message.answer("ℹ️ No tienes folios activos. Usa /permiso para iniciar uno.")
        return
    lista = []
    for folio in folios_usuario:
        if folio in timers_activos:
            mins = 120 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
            lista.append(f"• {folio} ({max(0, mins)} min restantes)")
        else:
            lista.append(f"• {folio} (sin timer)")
    await message.answer("📋 TUS FOLIOS:\n\n" + "\n".join(lista))

# ===== Info de costo =====
@dp.message(lambda m: m.text and any(p in m.text.lower() for p in ['costo','precio','cuanto','cuánto','depósito','deposito','pago','valor','monto']))
async def responder_costo(message: types.Message):
    await message.answer(f"💰 El permiso cuesta ${PRECIO_PERMISO} MXN. Inicia con /permiso.")

# ===== Fallback =====
@dp.message()
async def fallback(message: types.Message):
    opciones = [
        "🏛️ EDOMEX Permisos: usa /permiso para iniciar.",
        "📋 Servicio en línea: /permiso",
        "⚡ Genera tu documento oficial con /permiso",
        "🚗 Plataforma EDOMEX: /permiso"
    ]
    await message.answer(random.choice(opciones))

# ===================== FASTAPI (webhook y consulta) =====================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url, allowed_updates=["message"])
        print(f"[WEBHOOK] {webhook_url}")
        _keep_task = asyncio.create_task(keep_alive())
    else:
        print("[MODO] polling (sin webhook)")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Bot Permisos Estado de México", version="2.1.0")

@app.get("/")
async def health():
    return {
        "status": "running",
        "bot": "EDOMEX Permisos",
        "version": "2.1.0",
        "webhook": bool(BASE_URL),
        "timers_activos": len(timers_activos),
        "folio_prefix": FOLIO_PREFIX,
    }

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

@app.get("/status")
async def bot_status():
    try:
        me = await bot.get_me()
        return {"bot_active": True, "bot_username": me.username, "bot_id": me.id}
    except Exception as e:
        return {"bot_active": False, "error": str(e)}

@app.get("/consulta/{folio}")
async def consulta_qr(folio: str):
    folio = folio.strip()
    try:
        resp = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
        if not resp.data:
            return HTMLResponse(content=f"<h1>Folio {folio} no encontrado</h1>")
        reg = resp.data[0]
        fe = date.fromisoformat(reg["fecha_expedicion"])
        fv = date.fromisoformat(reg["fecha_vencimiento"])
        estado = "VIGENTE" if date.today() <= fv else "VENCIDO"
        html = f"""
        <h1>Permiso EDOMEX</h1>
        <p>Folio: {folio}</p>
        <p>Estado: {estado}</p>
        <p>Marca: {reg.get('marca','')}</p>
        <p>Serie: {reg.get('numero_serie','')}</p>
        <p>Vigencia hasta: {fv.strftime('%d/%m/%Y')}</p>
        """
        return HTMLResponse(content=html)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Error consultando folio {folio}: {e}</h1>")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
