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
PLANTILLA_PDF = "jalisco1.pdf"  # PDF principal (el completo)
PLANTILLA_BUENO = "jalisco.pdf"  # PDF simple (solo fecha y serie)

# Precio del permiso (añadido para compatibilidad con funciones CDMX)
PRECIO_PERMISO = 200

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT (AÑADIDO DESDE CDMX) ------------
timers_activos = {}  # {user_id: {"task": task, "folio": folio, "start_time": datetime}}

async def eliminar_folio_automatico(user_id: int, folio: str):
    """Elimina folio automáticamente después del tiempo límite"""
    try:
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario
        await bot.send_message(
            user_id,
            f"⏰ TIEMPO AGOTADO\n\n"
            f"El folio {folio} ha sido eliminado del sistema por falta de pago.\n\n"
            f"Para tramitar un nuevo permiso utilize /permiso"
        )
        
        # Limpiar timer
        if user_id in timers_activos:
            del timers_activos[user_id]
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Envía recordatorios de pago"""
    try:
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO JALISCO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: El costo es el mismo de siempre\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios"""
    async def timer_task():
        start_time = datetime.now()
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            # Verificar si el timer sigue activo
            if user_id not in timers_activos:
                return  # Timer cancelado (usuario pagó)
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(user_id, folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos (faltan 10)
        await asyncio.sleep(20 * 60)  # 20 minutos más
        if user_id in timers_activos:
            await enviar_recordatorio(user_id, folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        # Si llegamos aquí, se acabó el tiempo
        if user_id in timers_activos:
            await eliminar_folio_automatico(user_id, folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[user_id] = {
        "task": task,
        "folio": folio,
        "start_time": datetime.now()
    }

def cancelar_timer(user_id: int):
    """Cancela el timer cuando el usuario paga"""
    if user_id in timers_activos:
        timers_activos[user_id]["task"].cancel()
        del timers_activos[user_id]

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
    registros = supabase.table("folios_registrados").select("folio").eq("entidad","Jalisco").execute().data
    numeros = []
    for r in registros:
        f = r["folio"]
        try:
            numeros.append(int(f))
        except:
            continue
    siguiente = max(numeros) + 1 if numeros else 5908167415
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
    out = os.path.join(OUTPUT_DIR, f"{fol}_jalisco1.pdf")
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
    """Genera el PDF simple con fecha+hora y serie"""
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    
    # CORREGIDO: Definir fecha_hora_str antes de usarla
    fecha_hora_str = fecha.strftime("%d/%m/%Y %H:%M")
    
    # Imprimir fecha+hora y serie (CORREGIDO: usar serie en lugar de numero_serie)
    page.insert_text((380, 195), fecha_hora_str, fontsize=10, fontname="helv", color=(0, 0, 0))
    page.insert_text((380, 290), serie, fontsize=10, fontname="helv", color=(0, 0, 0))
    
    filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
    doc.save(filename)
    doc.close()
    
    return filename

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ Sistema Digital de Permisos JALISCO\n"
        "Servicio oficial automatizado para trámites vehiculares\n\n"
        "💰 Costo del permiso: El costo es el mismo de siempre\n"
        "⏰ Tiempo límite para pago: 2 horas\n"
        "📸 Métodos de pago: Transferencia bancaria y OXXO\n\n"
        "📋 Use /permiso para iniciar su trámite\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Cancelar timer anterior si existe
    cancelar_timer(message.from_user.id)
    
    await message.answer(
        f"🚗 TRÁMITE DE PERMISO JALISCO\n\n"
        f"📋 Costo: El costo es el mismo de siempre\n"
        f"⏰ Tiempo para pagar: 2 horas\n"
        f"📱 Concepto de pago: Su folio asignado\n\n"
        f"Al continuar acepta que su folio será eliminado si no paga en el tiempo establecido.\n\n"
        f"Comenzemos con la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca}\n\n"
        "Ahora indique la LÍNEA del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea}\n\n"
        "Proporcione el AÑO del vehículo (formato de 4 dígitos):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ El año debe contener exactamente 4 dígitos.\n"
            "Ejemplo válido: 2020, 2015, 2023\n\n"
            "Por favor, ingrese nuevamente el año:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO: {anio}\n\n"
        "Indique el NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ El número de serie parece incompleto.\n"
            "Verifique que haya ingresado todos los caracteres.\n\n"
            "Intente nuevamente:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE: {serie}\n\n"
        "Proporcione el NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor}\n\n"
        "Indique el COLOR del vehículo:"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR: {color}\n\n"
        "Finalmente, proporcione el NOMBRE COMPLETO del titular:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    
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

    await message.answer(
        f"🔄 PROCESANDO PERMISO JALISCO...\n\n"
        f"📄 Folio asignado: {datos['folio']}\n"
        f"👤 Titular: {nombre}\n\n"
        "Generando documentos oficiales..."
    )

    try:
        # Generar ambos PDFs
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

        # Enviar PDF principal
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📋 PERMISO PRINCIPAL JALISCO\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🏛️ Documento oficial con validez legal"
        )
        
        # Enviar PDF simple
        await message.answer_document(
            FSInputFile(p2),
            caption=f"📋 DOCUMENTO DE VERIFICACIÓN\n"
                   f"Serie: {datos['serie']}\n"
                   f"🔍 Comprobante adicional de autenticidad"
        )

        # Guardar en base de datos con estado PENDIENTE
        supabase.table("folios_registrados").insert({
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
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        # También en la tabla borradores (compatibilidad)
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

        # INICIAR TIMER DE PAGO
        await iniciar_timer_pago(message.from_user.id, datos['folio'])

        # Mensaje de instrucciones de pago con info bancaria real
        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"💵 Monto: El costo es el mismo de siempre\n"
            f"⏰ Tiempo límite: 2 horas\n\n"
            
            "🏦 TRANSFERENCIA BANCARIA:\n"
            "• Banco: AZTECA\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cuenta: 127180013037579543\n"
            "• Concepto: Permiso " + datos['folio'] + "\n\n"
            
            "🏪 PAGO EN OXXO:\n"
            "• Referencia: 2242170180385581\n"
            "• TARJETA SPIN\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cantidad exacta: El costo de siempre\n\n"
            
            f"📸 IMPORTANTE: Una vez realizado el pago, envíe la fotografía de su comprobante.\n\n"
            f"⚠️ ADVERTENCIA: Si no completa el pago en 2 horas, el folio {datos['folio']} será eliminado automáticamente del sistema."
        )
        
    except Exception as e:
        await message.answer(
            f"❌ ERROR EN EL SISTEMA\n\n"
            f"Se ha presentado un inconveniente técnico: {str(e)}\n\n"
            "Por favor, intente nuevamente con /permiso\n"
            "Si el problema persiste, contacte al soporte técnico."
        )
        print(f"Error: {e}")
    finally:
        await state.clear()

# ------------ CÓDIGO SECRETO ADMIN MEJORADO (AÑADIDO DESDE CDMX) ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    
    # Verificar formato: SERO + número de folio
    if len(texto) > 4:
        folio_admin = texto[4:]  # Quitar "SERO" del inicio
        
        # Buscar si hay un timer activo con ese folio
        user_con_folio = None
        for user_id, timer_info in timers_activos.items():
            if timer_info["folio"] == folio_admin:
                user_con_folio = user_id
                break
        
        if user_con_folio:
            # Cancelar timer
            cancelar_timer(user_con_folio)
            
            # Actualizar estado en base de datos
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            
            supabase.table("borradores_registros").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            
            await message.answer(
                f"✅ TIMER DEL FOLIO {folio_admin} SE DETUVO CON ÉXITO\n\n"
                f"🔐 Código admin ejecutado correctamente\n"
                f"⏰ Timer cancelado exitosamente\n"
                f"📄 Estado actualizado a VALIDADO_ADMIN\n"
                f"👤 Usuario ID: {user_con_folio}\n\n"
                f"El usuario ha sido notificado automáticamente."
            )
            
            # Notificar al usuario que su permiso está validado
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN\n\n"
                    f"📄 Folio: {folio_admin}\n"
                    f"Su permiso ha sido validado por administración.\n"
                    f"El documento está completamente activo para circular.\n\n"
                    f"Gracias por utilizar el Sistema Digital JALISCO."
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            await message.answer(
                f"❌ ERROR: EL TIMER SIGUE CORRIENDO\n\n"
                f"📄 Folio: {folio_admin}\n"
                f"⚠️ No se encontró ningún timer activo para este folio.\n\n"
                f"Posibles causas:\n"
                f"• El timer ya expiró automáticamente\n"
                f"• El usuario ya envió comprobante\n"
                f"• El folio no existe o es incorrecto\n"
                f"• El folio ya fue validado anteriormente"
            )
    else:
        await message.answer(
            "⚠️ FORMATO INCORRECTO\n\n"
            "Use el formato: SERO[número de folio]\n"
            "Ejemplo: SERO5908167415"
        )

# Handler para recibir comprobantes de pago (imágenes) - AÑADIDO DESDE CDMX
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    
    # Verificar si tiene timer activo
    if user_id not in timers_activos:
        await message.answer(
            "ℹ️ No se encontró ningún permiso pendiente de pago.\n\n"
            "Si desea tramitar un nuevo permiso, use /permiso"
        )
        return
    
    folio = timers_activos[user_id]["folio"]
    
    # Cancelar timer
    cancelar_timer(user_id)
    
    # Actualizar estado en base de datos
    supabase.table("folios_registrados").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    supabase.table("borradores_registros").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    await message.answer(
        f"✅ COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
        f"📄 Folio: {folio}\n"
        f"📸 Gracias por la imagen, este comprobante será revisado por un 2do filtro\n"
        f"⏰ Timer de pago detenido\n\n"
        f"🔍 Su comprobante está siendo verificado por nuestro equipo.\n"
        f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
        f"Gracias por utilizar el Sistema Digital JALISCO."
    )

# Handler para preguntas sobre costo/precio/depósito - AÑADIDO DESDE CDMX
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    async def responder_costo(message: types.Message):
    await message.answer(
        "💰 INFORMACIÓN DE COSTO\n\n"
        "El costo es el mismo de siempre.\n\n"
        "Para iniciar su trámite use /permiso"
    )

@dp.message()
async def fallback(message: types.Message):
    respuestas_elegantes = [
        "🏛️ Sistema Digital JALISCO. Para tramitar su permiso utilice /permiso",
        "📋 Servicio automatizado. Comando disponible: /permiso para iniciar trámite",
        "⚡ Sistema en línea. Use /permiso para generar su documento oficial",
        "🚗 Plataforma de permisos JALISCO. Inicie su proceso con /permiso"
    ]
    await message.answer(random.choice(respuestas_elegantes))

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

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
