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

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT - AUTOELIMINACIÓN A LAS 2 HORAS ------------
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [lista_de_folios_activos]}
pending_comprobantes = {}  # {user_id: folio} para usuarios esperando especificar folio

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después de 2 horas"""
    try:
        # Obtener user_id del folio
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
                f"⏰ NOTIFICACIÓN DE VENCIMIENTO - ESTADO DE JALISCO\n\n"
                f"Estimado usuario, lamentamos informarle que el folio {folio} ha sido eliminado del sistema por no haber completado el proceso de pago dentro del tiempo establecido.\n\n"
                f"Si desea tramitar un nuevo permiso de circulación, le invitamos cordialmente a utilizar el comando /permiso para iniciar un nuevo proceso.\n\n"
                f"Agradecemos su comprensión y quedamos a su disposición."
            )
        
        # Limpiar timers
        limpiar_timer_folio(folio)
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Envía recordatorios de pago elegantes"""
    try:
        mensaje_recordatorio = [
            f"🔔 RECORDATORIO CORTÉS DE PAGO - ESTADO DE JALISCO\n\n"
            f"Estimado usuario, nos permitimos recordarle amablemente que su trámite requiere atención.\n\n"
            f"📄 Folio de referencia: {folio}\n"
            f"⏱️ Tiempo restante para completar el pago: {minutos_restantes} minutos\n"
            f"💰 Concepto: Permiso de circulación temporal\n\n"
            f"Le sugerimos enviar la fotografía de su comprobante de pago a la brevedad posible para validar su trámite.\n\n"
            f"Agradecemos su atención y colaboración.",
            
            f"⚡ NOTIFICACIÓN DE SEGUIMIENTO - GOBIERNO DE JALISCO\n\n"
            f"Distinguido ciudadano, le recordamos cordialmente sobre su trámite en proceso.\n\n"
            f"📋 Número de expediente: {folio}\n"
            f"🕐 Tiempo disponible restante: {minutos_restantes} minutos\n"
            f"🏛️ Servicio: Expedición de permiso vehicular\n\n"
            f"Para completar su proceso, sírvase enviar la imagen de su comprobante de pago.\n\n"
            f"Quedamos atentos a su respuesta."
        ]
        await bot.send_message(user_id, random.choice(mensaje_recordatorio))
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    """Inicia el timer de 2 horas para eliminación automática con recordatorios"""
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id}")
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            # Verificar si el timer sigue activo
            if folio not in timers_activos:
                return  # Timer cancelado (usuario pagó)
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(user_id, folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos (faltan 10)
        await asyncio.sleep(20 * 60)  # 20 minutos más
        if folio in timers_activos:
            await enviar_recordatorio(user_id, folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        # Si llegamos aquí, se acabó el tiempo - eliminar
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

# ============ FUNCIÓN GENERAR FOLIO JALISCO CON VERIFICACIÓN ROBUSTA ============
def generar_folio_jalisco():
    """
    CORREGIDO: Busca el siguiente folio disponible verificando también en tiempo real
    """
    max_intentos = 0
    
    for intento in range(max_intentos):
        try:
            # Obtener folios existentes en CADA intento (tiempo real)
            registros = supabase.table("folios_registrados").select("folio").eq("entidad", "Jalisco").execute().data
            
            folios_existentes = set()
            numeros_validos = []
            
            for registro in registros:
                folio_str = registro["folio"]
                try:
                    numero = int(folio_str)
                    folios_existentes.add(numero)
                    numeros_validos.append(numero)
                except (ValueError, TypeError):
                    continue
            
            if numeros_validos:
                # Filtrar solo folios que empiecen desde 7100167415 o mayor
                folios_validos_rango = [f for f in numeros_validos if f >= 7100167415]
                if folios_validos_rango:
                    siguiente_candidato = max(folios_validos_rango) + 1
                else:
                    siguiente_candidato = 7100167415
            else:
                siguiente_candidato = 7100167415
            
            # Buscar el siguiente disponible
            while siguiente_candidato in folios_existentes:
                siguiente_candidato += 1
            
            print(f"[INTENTO {intento + 1}] Folio candidato: {siguiente_candidato}")
            return str(siguiente_candidato)
            
        except Exception as e:
            print(f"[ERROR INTENTO {intento + 1}] {e}")
            if intento == max_intentos - 1:
                # Último intento - usar timestamp único
                return str(int(time.time() * 1000000))  # Microsegundos para máxima unicidad
            continue
    
    # Fallback final
    return str(int(time.time() * 1000000))

# ============ GENERADOR DE FOLIOS INTELIGENTE ============
import random
from supabase import create_client, Client

# Rango de folios consecutivos
FOLIO_INICIO = 7200005678
FOLIO_FIN = 999999999

def obtener_ultimo_folio_usado():
    """
    Obtiene el último folio usado de la base de datos
    para continuar la secuencia consecutiva
    """
    try:
        # Buscar el folio más alto en el rango específico
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .gte("folio", FOLIO_INICIO) \
            .lte("folio", FOLIO_FIN) \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()
        
        if response.data:
            ultimo_folio = int(response.data[0]["folio"])
            print(f"[INFO] Último folio encontrado: {ultimo_folio}")
            return ultimo_folio
        else:
            print(f"[INFO] No hay folios previos, empezando desde: {FOLIO_INICIO}")
            return FOLIO_INICIO - 1  # Para que el siguiente sea FOLIO_INICIO
            
    except Exception as e:
        print(f"[ERROR] Al obtener último folio: {e}")
        return FOLIO_INICIO - 1

def generar_folio_consecutivo():
    """
    Genera el siguiente folio consecutivo disponible
    """
    ultimo_folio = obtener_ultimo_folio_usado()
    siguiente_folio = ultimo_folio + 1
    
    # Verificar que no exceda el límite máximo
    if siguiente_folio > FOLIO_FIN:
        print(f"[ADVERTENCIA] Se alcanzó el límite máximo de folios: {FOLIO_FIN}")
        # Opcional: buscar gaps en la secuencia
        return buscar_folio_disponible_en_gaps()
    
    return str(siguiente_folio).zfill(10)  # Asegurar formato de 10 dígitos

def buscar_folio_disponible_en_gaps():
    """
    Busca folios disponibles en gaps de la secuencia
    (útil si se alcanza el límite o hay huecos)
    """
    try:
        # Obtener todos los folios registrados en el rango
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .gte("folio", FOLIO_INICIO) \
            .lte("folio", FOLIO_FIN) \
            .order("folio") \
            .execute()
        
        folios_usados = set(int(row["folio"]) for row in response.data)
        
        # Buscar el primer gap disponible
        for folio_candidato in range(FOLIO_INICIO, FOLIO_FIN + 1):
            if folio_candidato not in folios_usados:
                print(f"[GAP ENCONTRADO] Folio disponible: {folio_candidato}")
                return str(folio_candidato).zfill(10)
        
        print("[ERROR] No hay folios disponibles en todo el rango")
        return None
        
    except Exception as e:
        print(f"[ERROR] Al buscar gaps: {e}")
        return None

def verificar_folio_existe(folio):
    """
    Verifica si un folio ya existe en la base de datos
    """
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("folio", folio) \
            .execute()
        
        return len(response.data) > 0
        
    except Exception as e:
        print(f"[ERROR] Al verificar folio {folio}: {e}")
        return True  # Asumir que existe para evitar duplicados

def buscar_siguiente_folio_disponible(folio_inicial):
    """
    Busca el siguiente folio disponible desde un punto inicial
    """
    folio_actual = int(folio_inicial)
    intentos = 0
    max_intentos = 1000  # Evitar bucles infinitos
    
    while intentos < max_intentos and folio_actual <= FOLIO_FIN:
        if not verificar_folio_existe(str(folio_actual).zfill(10)):
            return str(folio_actual).zfill(10)
        
        folio_actual += 1
        intentos += 1
    
    print(f"[ERROR] No se encontró folio disponible después de {max_intentos} intentos")
    return None

# ============ FUNCIÓN GUARDAR MEJORADA ============
async def guardar_folio_inteligente(datos, user_id, username):
    """
    Guarda el folio con lógica inteligente de recuperación
    """
    max_intentos = 5
    
    for intento in range(max_intentos):
        try:
            # En el primer intento, usar el folio consecutivo normal
            if intento == 0:
                folio_a_usar = generar_folio_consecutivo()
            else:
                # En reintentos, buscar el siguiente disponible
                print(f"[REINTENTO {intento}] Buscando siguiente folio disponible...")
                folio_a_usar = buscar_siguiente_folio_disponible(datos["folio"])
                
                if not folio_a_usar:
                    print("[ERROR FATAL] No se encontró ningún folio disponible")
                    return False
            
            # Actualizar el folio en los datos
            datos["folio"] = folio_a_usar
            print(f"[INTENTO {intento + 1}] Probando folio: {folio_a_usar}")
            
            # Intentar guardar
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
            
            print(f"[ÉXITO] ✅ Folio {datos['folio']} guardado correctamente")
            return True
            
        except Exception as e:
            error_msg = str(e).lower()
            
            if "duplicate" in error_msg or "unique constraint" in error_msg or "23505" in error_msg:
                print(f"[DUPLICADO] ⚠️ Folio {datos['folio']} ya existe, buscando otro...")
                continue  # No se queda como pendejo, busca otro
            else:
                print(f"[ERROR DIFERENTE] ❌ {e}")
                return False
    
    print(f"[ERROR FATAL] ❌ Se agotaron los {max_intentos} intentos")
    return False

# ============ FUNCIÓN PRINCIPAL ACTUALIZADA ============
def generar_folio_jalisco():
    """
    Función principal que reemplaza la anterior
    Genera folios consecutivos desde 7200005678
    """
    return generar_folio_consecutivo()

# ============ ESTADÍSTICAS Y MONITOREO ============
def obtener_estadisticas_folios():
    """
    Obtiene estadísticas del uso de folios
    """
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .gte("folio", FOLIO_INICIO) \
            .lte("folio", FOLIO_FIN) \
            .execute()
        
        total_usados = len(response.data)
        total_disponibles = (FOLIO_FIN - FOLIO_INICIO + 1) - total_usados
        porcentaje_usado = (total_usados / (FOLIO_FIN - FOLIO_INICIO + 1)) * 100
        
        print(f"""
📊 ESTADÍSTICAS DE FOLIOS:
• Rango: {FOLIO_INICIO:,} - {FOLIO_FIN:,}
• Total disponibles: {FOLIO_FIN - FOLIO_INICIO + 1:,}
• Folios usados: {total_usados:,}
• Folios libres: {total_disponibles:,}
• Porcentaje usado: {porcentaje_usado:.2f}%
        """)
        
        return {
            "total_disponibles": FOLIO_FIN - FOLIO_INICIO + 1,
            "usados": total_usados,
            "libres": total_disponibles,
            "porcentaje_usado": porcentaje_usado
        }
        
    except Exception as e:
        print(f"[ERROR] Al obtener estadísticas: {e}")
        return None

# ============ EJEMPLO DE USO ============
"""
# Reemplazar la función anterior por:
resultado = await guardar_folio_inteligente(datos, user_id, username)

if resultado:
    print("Folio guardado exitosamente!")
else:
    print("Error al guardar el folio")

# Para ver estadísticas:
obtener_estadisticas_folios()
"""
    
# ============ FUNCIÓN FOLIO REPRESENTATIVO CON PERSISTENCIA ============
def obtener_folio_representativo():
    """Obtiene folio representativo, manteniendo persistencia entre reinicios"""
    try:
        # Intentar leer desde archivo local
        with open("folio_representativo.txt") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        # Si no existe archivo, empezar desde valor base
        folio_inicial = 501997
        with open("folio_representativo.txt", "w") as f:
            f.write(str(folio_inicial))
        print(f"[REPRESENTATIVO] Archivo creado con valor inicial: {folio_inicial}")
        return folio_inicial
    except Exception as e:
        print(f"[ERROR] Leyendo folio representativo: {e}")
        return 501997  # Valor por defecto

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
        return folio_actual + 1  # Continuar aunque falle el guardado

# ============ FUNCIÓN GENERAR CÓDIGO INE (PDF417) ============
def generar_codigo_ine(contenido, ruta_salida):
    """Genera código PDF417 estilo INE"""
    try:
        codes = pdf417gen.encode(contenido, columns=6, security_level=5)
        image = pdf417gen.render_image(codes)
        image.save(ruta_salida)
        print(f"[PDF417] Código generado: {ruta_salida}")
    except Exception as e:
        print(f"[ERROR] Generando PDF417: {e}")
        # Crear imagen en blanco como fallback
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
    ahora_mexico = datetime.now(zona_mexico)
    
    # Crear carpeta de salida
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_jalisco1.pdf")
    
    try:
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
        fol_representativo = obtener_folio_representativo()
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

        # --- Insertar imagen en tamaño FIJO ---
        pg.insert_image(fitz.Rect(937.65, 75, 1168.955, 132), filename=ine_img_path, 
                        keep_proportion=False, overlay=True)

        doc.save(out)
        doc.close()
        print(f"[PDF] Generado exitosamente: {out}")
        
    except Exception as e:
        print(f"[ERROR] Generando PDF principal: {e}")
        # Crear PDF mínimo como fallback
        doc_fallback = fitz.open()
        page = doc_fallback.new_page()
        page.insert_text((50, 50), f"ERROR - Folio: {fol}", fontsize=12)
        doc_fallback.save(out)
        doc_fallback.close()
    
    return out

# ============ PDF BUENO (SIMPLE - SOLO FECHA Y SERIE) ============
def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    """Genera el PDF simple con fecha+hora y serie"""
    try:
        doc = fitz.open(PLANTILLA_BUENO)
        page = doc[0]
        
        # Crear fecha y hora string
        fecha_hora_str = fecha.strftime("%d/%m/%Y %H:%M")
        
        # Imprimir fecha+hora y serie
        page.insert_text((380, 195), fecha_hora_str, fontsize=10, fontname="helv", color=(0, 0, 0))
        page.insert_text((380, 290), serie, fontsize=10, fontname="helv", color=(0, 0, 0))
        
        filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
        doc.save(filename)
        doc.close()
        
        return filename
    except Exception as e:
        print(f"[ERROR] Generando PDF bueno: {e}")
        return None

# ------------ HANDLERS CON DIÁLOGOS PROFESIONALES Y ELEGANTES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    frases_start = [
        "🏛️ BIENVENIDO AL SISTEMA DIGITAL DEL ESTADO DE JALISCO\n"
        "Plataforma gubernamental oficial para la gestión de permisos de circulación vehicular\n\n"
        "Nos complace atenderle en este servicio automatizado de excelencia, diseñado para brindarle la máxima comodidad y eficiencia en sus trámites.\n\n"
        "📋 Inversión por servicio: Tarifa oficial establecida\n"
        "⏰ Plazo para liquidación: 2 horas a partir de la emisión\n"
        "💳 Modalidades de pago: Transferencia bancaria y establecimientos OXXO\n\n"
        "Para dar inicio a su trámite, le invitamos cordialmente a utilizar el comando /permiso\n\n"
        "⚠️ NOTA IMPORTANTE: Le informamos respetuosamente que su folio será eliminado automáticamente del sistema si no completa el proceso de pago dentro del tiempo establecido.",
        
        "🌟 SISTEMA GUBERNAMENTAL DE JALISCO - SERVICIO DIGITAL\n"
        "Distinguido ciudadano, sea usted bienvenido a nuestra plataforma de servicios digitales\n\n"
        "Es un honor poder asistirle en la tramitación de su permiso de circulación a través de este moderno sistema que hemos diseñado especialmente para su conveniencia.\n\n"
        "💰 Concepto: Permiso temporal de circulación\n"
        "🕐 Tiempo disponible para pago: 120 minutos\n"
        "🏪 Puntos de pago autorizados: Red OXXO y transferencias bancarias\n\n"
        "Le solicitamos amablemente iniciar su proceso mediante el comando /permiso\n\n"
        "📢 AVISO CORTÉS: Su folio será eliminado de manera automática en caso de no completar el pago en el tiempo estipulado."
    ]
    await message.answer(random.choice(frases_start))

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Verificar folios activos del usuario
    folios_activos = obtener_folios_usuario(message.from_user.id)
    
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTUALMENTE EN PROCESO: {', '.join(folios_activos)}\n(Cada expediente mantiene su cronómetro independiente de 2 horas)"
    
    frases_inicio = [
        f"🚗 SOLICITUD DE PERMISO DE CIRCULACIÓN - ESTADO DE JALISCO\n\n"
        f"Estimado usuario, nos da mucho gusto poder atenderle en este momento. A continuación procederemos con la captura de los datos de su vehículo para la expedición de su permiso temporal.\n\n"
        f"📋 Concepto del trámite: Permiso de circulación temporal\n"
        f"💰 Inversión requerida: Según tarifa oficial vigente\n"
        f"⏰ Plazo para completar el pago: 2 horas\n\n"
        f"Al continuar con este proceso, usted acepta expresamente que su folio será eliminado automáticamente si no efectúa el pago correspondiente dentro del tiempo establecido."
        f"{mensaje_folios}\n\n"
        f"Para dar inicio, le solicitamos muy amablemente proporcionar la MARCA de su vehículo:",
        
        f"🏛️ TRÁMITE OFICIAL DE PERMISO VEHICULAR - JALISCO\n\n"
        f"Distinguido ciudadano, es un honor poder asistirle en la gestión de su documentación vehicular. Nuestro sistema le guiará paso a paso para completar su trámite de manera eficiente y segura.\n\n"
        f"💼 Servicio solicitado: Expedición de permiso temporal\n"
        f"💵 Inversión del servicio: Conforme a tarifas gubernamentales\n"
        f"🕐 Ventana de tiempo para pago: 120 minutos exactos\n\n"
        f"Mediante la continuación de este proceso, usted confirma su conocimiento y aceptación de las políticas de eliminación automática por falta de pago."
        f"{mensaje_folios}\n\n"
        f"Como primer paso, le rogamos tenga la gentileza de indicar la MARCA del vehículo a registrar:"
    ]
    await message.answer(random.choice(frases_inicio))
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    
    if not marca or len(marca) < 2:
        frases_error = [
            "⚠️ INFORMACIÓN REQUERIDA INCOMPLETA\n\n"
            "Estimado usuario, le solicitamos amablemente proporcionar una marca válida para su vehículo. La información debe contener al menos 2 caracteres para ser procesada correctamente.\n\n"
            "Ejemplos de marcas válidas: NISSAN, TOYOTA, HONDA, VOLKSWAGEN, FORD, CHEVROLET\n\n"
            "Le rogamos tenga la gentileza de intentar nuevamente:",
            
            "❌ DATO INSUFICIENTE PARA EL PROCESAMIENTO\n\n"
            "Distinguido usuario, para poder continuar con su trámite necesitamos que nos proporcione la marca completa de su vehículo. La información ingresada debe ser específica y clara.\n\n"
            "Marcas de ejemplo: BMW, AUDI, MAZDA, KIA, HYUNDAI, JEEP\n\n"
            "Le agradecemos su colaboración y le pedimos reintentar:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    await state.update_data(marca=marca)
    
    frases_marca = [
        f"✅ MARCA REGISTRADA EXITOSAMENTE: {marca}\n\n"
        f"Excelente información proporcionada. Su marca ha sido capturada correctamente en nuestro sistema.\n\n"
        f"Como siguiente paso, le solicitamos muy cordialmente proporcionar la LÍNEA o MODELO específico de su vehículo:",
        
        f"📝 MARCA CONFIRMADA EN EL SISTEMA: {marca}\n\n"
        f"Perfecto. La información de la marca ha sido validada y almacenada satisfactoriamente.\n\n"
        f"Continuando con el proceso, le rogamos tenga la amabilidad de especificar la LÍNEA/MODELO de su unidad vehicular:",
        
        f"🎯 MARCA VALIDADA Y PROCESADA: {marca}\n\n"
        f"Muy bien. Los datos han sido ingresados correctamente al expediente.\n\n"
        f"Prosiguiendo con la captura de información, le pedimos gentilmente indicar la LÍNEA o MODELO del vehículo:"
    ]
    await message.answer(random.choice(frases_marca))
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    
    if not linea or len(linea) < 1:
        frases_error = [
            "⚠️ INFORMACIÓN REQUERIDA PARA CONTINUAR\n\n"
            "Estimado usuario, para poder proceder con su trámite necesitamos que nos proporcione la línea o modelo específico de su vehículo.\n\n"
            "Ejemplos de líneas válidas: SENTRA, TSURU, AVEO, JETTA, CIVIC, COROLLA\n\n"
            "Le agradecemos su comprensión y le solicitamos reintentar:",
            
            "❌ DATO FALTANTE EN EL EXPEDIENTE\n\n"
            "Distinguido usuario, la información de línea/modelo es indispensable para la emisión de su permiso.\n\n"
            "Referencias de modelos: FOCUS, CRUZE, ALTIMA, VERSA, MARCH, TIIDA\n\n"
            "Le rogamos proporcionar esta información:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    await state.update_data(linea=linea)
    
    frases_linea = [
        f"✅ LÍNEA CONFIRMADA SATISFACTORIAMENTE: {linea}\n\n"
        f"Excelente. La información del modelo ha sido registrada correctamente en su expediente.\n\n"
        f"Como siguiente paso en el proceso, le solicitamos cordialmente proporcionar el AÑO de fabricación del vehículo (formato de 4 dígitos):",
        
        f"📋 MODELO REGISTRADO EN EL SISTEMA: {linea}\n\n"
        f"Perfecto. Los datos del modelo han sido validados y almacenados exitosamente.\n\n"
        f"Continuando con la captura, le rogamos especificar el AÑO de manufactura del vehículo (4 dígitos):",
        
        f"🎯 LÍNEA VALIDADA Y PROCESADA: {linea}\n\n"
        f"Muy bien. La información ha sido ingresada correctamente al sistema.\n\n"
        f"Prosiguiendo con el trámite, le pedimos gentilmente proporcionar el AÑO de fabricación (formato YYYY):"
    ]
    await message.answer(random.choice(frases_linea))
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    
    if not anio.isdigit() or len(anio) != 4:
        frases_error = [
            "⚠️ FORMATO DE AÑO INCORRECTO\n\n"
            "Estimado usuario, le solicitamos respetuosamente proporcionar el año de fabricación en formato de 4 dígitos numéricos.\n\n"
            "Ejemplos válidos: 2020, 2015, 2023, 2018, 2019, 2024\n\n"
            "Le agradecemos su comprensión y le pedimos intentar nuevamente:",
            
            "❌ DATO NUMÉRICO REQUERIDO\n\n"
            "Distinguido usuario, el año de fabricación debe contener exactamente 4 dígitos numéricos para ser procesado correctamente por nuestro sistema.\n\n"
            "Formatos correctos: 2016, 2017, 2022, 2021, 2025\n\n"
            "Le rogamos corregir el formato:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    anio_num = int(anio)
    if anio_num < 1980 or anio_num > datetime.now().year + 1:
        frases_error_rango = [
            f"⚠️ AÑO FUERA DEL RANGO PERMITIDO\n\n"
            f"Estimado usuario, el sistema requiere que el año de fabricación esté comprendido entre 1980 y {datetime.now().year + 1} para ser procesado correctamente.\n\n"
            f"Año proporcionado: {anio} (no se encuentra en el rango válido)\n\n"
            f"Le solicitamos amablemente verificar la información e intentar nuevamente:",
            
            f"❌ RANGO DE AÑOS NO VÁLIDO\n\n"
            f"Distinguido usuario, nuestro sistema acepta únicamente años entre 1980 y {datetime.now().year + 1} inclusive.\n\n"
            f"Su entrada: {anio} (fuera de los límites establecidos)\n\n"
            f"Le agradecemos su comprensión y le pedimos corregir:"
        ]
        await message.answer(random.choice(frases_error_rango))
        return
    
    await state.update_data(anio=anio)
    
    frases_anio = [
        f"✅ AÑO VERIFICADO Y CONFIRMADO: {anio}\n\n"
        f"Excelente información proporcionada. El año de fabricación ha sido validado correctamente en nuestro sistema.\n\n"
        f"Continuando con el proceso, le solicitamos muy cordialmente proporcionar el NÚMERO DE SERIE del vehículo:",
        
        f"📅 AÑO REGISTRADO EXITOSAMENTE: {anio}\n\n"
        f"Perfecto. La información del año ha sido capturada y verificada satisfactoriamente.\n\n"
        f"Como siguiente paso, le rogamos tenga la gentileza de especificar el NÚMERO DE SERIE del vehículo:",
        
        f"🎯 AÑO VALIDADO EN EL SISTEMA: {anio}\n\n"
        f"Muy bien. Los datos han sido procesados correctamente en su expediente.\n\n"
        f"Prosiguiendo con la captura, le pedimos amablemente proporcionar el NÚMERO DE SERIE:"
    ]
    await message.answer(random.choice(frases_anio))
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    
    if len(serie) < 5:
        frases_error = [
            "⚠️ NÚMERO DE SERIE INCOMPLETO\n\n"
            "Estimado usuario, el número de serie proporcionado parece estar incompleto. Para garantizar la correcta emisión de su permiso, necesitamos que el número contenga al menos 5 caracteres.\n\n"
            "Le sugerimos revisar la documentación oficial de su vehículo (tarjeta de circulación) para verificar que haya ingresado la información completa.\n\n"
            "Le agradecemos su colaboración e intentar nuevamente:",
            
            "❌ SERIE INSUFICIENTE PARA PROCESAMIENTO\n\n"
            "Distinguido usuario, nuestro sistema requiere un mínimo de 5 caracteres para validar correctamente el número de serie del vehículo.\n\n"
            "Le recomendamos consultar su documentación vehicular oficial para asegurar la información correcta.\n\n"
            "Le rogamos proporcionar la información completa:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(serie) > 25:
        frases_error_largo = [
            "⚠️ NÚMERO DE SERIE EXCESIVO\n\n"
            "Estimado usuario, el número de serie no puede exceder los 25 caracteres según nuestros estándares de sistema.\n\n"
            "Le solicitamos verificar que no haya incluido información adicional y proporcionar únicamente el número de serie del vehículo.\n\n"
            "Le agradecemos intentar nuevamente:",
            
            "❌ LÍMITE DE CARACTERES SUPERADO\n\n"
            "Distinguido usuario, el sistema acepta máximo 25 caracteres para el número de serie.\n\n"
            "Le rogamos revisar que la información corresponda exclusivamente al número de serie oficial.\n\n"
            "Le pedimos ajustar la información:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    await state.update_data(serie=serie)
    
    frases_serie = [
        f"✅ NÚMERO DE SERIE CAPTURADO: {serie}\n\n"
        f"Excelente. La información del número de serie ha sido registrada correctamente en su expediente.\n\n"
        f"Como siguiente paso en el proceso, le solicitamos cordialmente proporcionar el NÚMERO DE MOTOR del vehículo:",
        
        f"📝 SERIE REGISTRADA EN EL SISTEMA: {serie}\n\n"
        f"Perfecto. Los datos de la serie han sido validados y almacenados satisfactoriamente.\n\n"
        f"Continuando con la captura, le rogamos especificar el NÚMERO DE MOTOR del vehículo:",
        
        f"🎯 SERIE VALIDADA Y PROCESADA: {serie}\n\n"
        f"Muy bien. La información ha sido ingresada correctamente al sistema.\n\n"
        f"Prosiguiendo con el trámite, le pedimos gentilmente proporcionar el NÚMERO DE MOTOR:"
    ]
    await message.answer(random.choice(frases_serie))
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    
    if len(motor) < 5:
        frases_error = [
            "⚠️ NÚMERO DE MOTOR INCOMPLETO\n\n"
            "Estimado usuario, el número de motor proporcionado parece requerir información adicional. Para asegurar la correcta emisión de su permiso, necesitamos que contenga al menos 5 caracteres.\n\n"
            "Le sugerimos consultar la documentación oficial de su vehículo para verificar la información completa.\n\n"
            "Le agradecemos su colaboración e intentar nuevamente:",
            
            "❌ MOTOR INSUFICIENTE PARA VALIDACIÓN\n\n"
            "Distinguido usuario, nuestro sistema requiere un mínimo de 5 caracteres para procesar correctamente el número de motor.\n\n"
            "Le recomendamos revisar la tarjeta de circulación para obtener el dato completo.\n\n"
            "Le rogamos proporcionar la información completa:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(motor) > 25:
        frases_error_largo = [
            "⚠️ NÚMERO DE MOTOR EXCESIVO\n\n"
            "Estimado usuario, el número de motor no puede superar los 25 caracteres según los parámetros del sistema.\n\n"
            "Le solicitamos verificar que corresponda únicamente al número de motor oficial del vehículo.\n\n"
            "Le agradecemos intentar nuevamente:",
            
            "❌ LÍMITE MÁXIMO SUPERADO\n\n"
            "Distinguido usuario, el sistema procesa máximo 25 caracteres para el número de motor.\n\n"
            "Le rogamos ajustar la información para que corresponda exclusivamente al número oficial.\n\n"
            "Le pedimos corregir la entrada:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    await state.update_data(motor=motor)
    
    frases_motor = [
        f"✅ NÚMERO DE MOTOR REGISTRADO: {motor}\n\n"
        f"Excelente información proporcionada. El número de motor ha sido capturado correctamente en nuestro sistema.\n\n"
        f"Continuando con el proceso, le solicitamos muy cordialmente especificar el COLOR del vehículo:",
        
        f"📝 MOTOR CAPTURADO EN EL SISTEMA: {motor}\n\n"
        f"Perfecto. La información del motor ha sido validada y almacenada exitosamente.\n\n"
        f"Como siguiente paso, le rogamos tenga la gentileza de indicar el COLOR del vehículo:",
        
        f"🎯 MOTOR VALIDADO Y PROCESADO: {motor}\n\n"
        f"Muy bien. Los datos han sido ingresados correctamente al expediente.\n\n"
        f"Prosiguiendo con la captura, le pedimos amablemente especificar el COLOR del vehículo:"
    ]
    await message.answer(random.choice(frases_motor))
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    
    if not color or len(color) < 2:
        frases_error = [
            "⚠️ COLOR REQUERIDO PARA CONTINUAR\n\n"
            "Estimado usuario, la especificación del color del vehículo es indispensable para completar su trámite.\n\n"
            "Ejemplos de colores válidos: BLANCO, AZUL, ROJO, NEGRO, GRIS, VERDE, AMARILLO\n\n"
            "Le solicitamos amablemente proporcionar esta información:",
            
            "❌ INFORMACIÓN DE COLOR FALTANTE\n\n"
            "Distinguido usuario, necesitamos que nos indique el color de su vehículo para proceder con la emisión del permiso.\n\n"
            "Referencias válidas: PLATA, CAFÉ, NARANJA, MORADO, ROSA, DORADO\n\n"
            "Le rogamos proporcionar este dato:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(color) > 20:
        frases_error_largo = [
            "⚠️ DESCRIPCIÓN DE COLOR EXCESIVA\n\n"
            "Estimado usuario, la descripción del color no puede exceder los 20 caracteres para ser procesada correctamente.\n\n"
            "Le sugerimos utilizar descripciones simples como: AZUL MARINO, GRIS OXFORD, VERDE LIMA\n\n"
            "Le agradecemos intentar nuevamente:",
            
            "❌ LÍMITE DE CARACTERES PARA COLOR\n\n"
            "Distinguido usuario, el sistema acepta máximo 20 caracteres para la descripción del color.\n\n"
            "Le recomendamos simplificar la descripción manteniendo la información esencial.\n\n"
            "Le rogamos ajustar la entrada:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    await state.update_data(color=color)
    
    frases_color = [
        f"✅ COLOR CONFIRMADO SATISFACTORIAMENTE: {color}\n\n"
        f"Excelente. La información del color ha sido registrada correctamente en su expediente.\n\n"
        f"Como paso final del proceso, le solicitamos muy cordialmente proporcionar el NOMBRE COMPLETO del propietario del vehículo:",
        
        f"🎨 COLOR REGISTRADO EN EL SISTEMA: {color}\n\n"
        f"Perfecto. Los datos del color han sido validados y almacenados exitosamente.\n\n"
        f"Para completar su trámite, le rogamos tenga la gentileza de proporcionar el NOMBRE COMPLETO del titular:",
        
        f"🎯 COLOR VALIDADO Y PROCESADO: {color}\n\n"
        f"Muy bien. La información ha sido capturada correctamente en el sistema.\n\n"
        f"Finalizando la captura de datos, le pedimos amablemente especificar el NOMBRE COMPLETO del propietario del vehículo:"
    ]
    await message.answer(random.choice(frases_color))
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    
    # Validar nombre
    if len(nombre) < 5:
        frases_error = [
            "⚠️ NOMBRE COMPLETO REQUERIDO\n\n"
            "Estimado usuario, para la correcta emisión de su permiso necesitamos que proporcione el nombre completo del titular, incluyendo nombre(s) y apellido(s).\n\n"
            "Ejemplo de formato correcto: JUAN PÉREZ GARCÍA\n\n"
            "Le solicitamos amablemente intentar nuevamente:",
            
            "❌ INFORMACIÓN NOMINAL INSUFICIENTE\n\n"
            "Distinguido usuario, el sistema requiere el nombre completo del propietario para procesar adecuadamente su trámite.\n\n"
            "Formato sugerido: MARÍA GONZÁLEZ LÓPEZ\n\n"
            "Le rogamos completar esta información:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(nombre) > 60:
        frases_error_largo = [
            "⚠️ NOMBRE EXCEDE LÍMITE PERMITIDO\n\n"
            "Estimado usuario, el nombre completo no puede superar los 60 caracteres según los parámetros del sistema.\n\n"
            "Le solicitamos verificar la información y simplificar si es necesario.\n\n"
            "Le agradecemos intentar nuevamente:",
            
            "❌ LÍMITE MÁXIMO DE CARACTERES\n\n"
            "Distinguido usuario, nuestro sistema acepta máximo 60 caracteres para el nombre completo.\n\n"
            "Le rogamos ajustar la información manteniendo los datos esenciales.\n\n"
            "Le pedimos corregir la entrada:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    # Verificar que tenga al menos dos palabras
    palabras = nombre.split()
    if len(palabras) < 2:
        frases_error_palabras = [
            "⚠️ NOMBRE Y APELLIDO REQUERIDOS\n\n"
            "Estimado usuario, le solicitamos proporcionar al menos el nombre y un apellido para proceder correctamente.\n\n"
            "Ejemplo mínimo requerido: MARÍA GONZÁLEZ\n\n"
            "Le agradecemos completar esta información:",
            
            "❌ DATOS NOMINALES INCOMPLETOS\n\n"
            "Distinguido usuario, necesitamos como mínimo el nombre y un apellido del titular.\n\n"
            "Formato mínimo: JOSÉ MARTÍNEZ\n\n"
            "Le rogamos proporcionar la información completa:"
        ]
        await message.answer(random.choice(frases_error_palabras))
        return
    
    datos["nombre"] = nombre
    
    # Generar folio único de Jalisco con continuidad
    datos["folio"] = generar_folio_jalisco()

    # Fechas
    hoy = datetime.now()
    vigencia_dias = 30
    fecha_ven = hoy + timedelta(days=vigencia_dias)
    
    datos["fecha_exp"] = hoy
    datos["fecha_ven"] = fecha_ven
    
    # Formato de fecha elegante en español
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")

    try:
        frases_procesando = [
            f"🔄 PROCESANDO DOCUMENTACIÓN OFICIAL DEL ESTADO DE JALISCO...\n\n"
            f"Estimado usuario, nos complace informarle que su solicitud ha sido recibida exitosamente y se encuentra en procesamiento.\n\n"
            f"📄 Folio de expediente asignado: {datos['folio']}\n"
            f"🚗 Vehículo registrado: {datos['marca']} {datos['linea']} modelo {datos['anio']}\n"
            f"👤 Titular del permiso: {nombre}\n\n"
            f"Nuestro sistema está generando su documentación oficial. Le solicitamos unos momentos mientras completamos el proceso...",
            
            f"⚡ GENERANDO DOCUMENTOS GUBERNAMENTALES OFICIALES...\n\n"
            f"Distinguido ciudadano, su trámite ha sido recibido con beneplácito y se encuentra siendo procesado por nuestros sistemas especializados.\n\n"
            f"🆔 Código único de identificación: {datos['folio']}\n"
            f"🚙 Unidad vehicular: {datos['marca']} {datos['linea']} ({datos['anio']})\n"
            f"👥 Solicitante: {nombre}\n\n"
            f"En este momento estamos generando su documentación oficial del Estado de Jalisco. Le agradecemos su paciencia..."
        ]
        await message.answer(random.choice(frases_procesando))
        
        # Generar ambos PDFs
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

        # Enviar PDF principal
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📋 PERMISO OFICIAL DE CIRCULACIÓN - ESTADO DE JALISCO\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días calendario\n"
                   f"🏛️ Documento expedido con plena validez oficial gubernamental"
        )
        
        # Enviar PDF complementario si se generó correctamente
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"🧾 DOCUMENTO COMPLEMENTARIO DE VERIFICACIÓN\n"
                       f"Serie del vehículo: {datos['serie']}\n"
                       f"📋 Comprobante adicional de autenticidad y respaldo"
            )

        # Guardar en base de datos con estado PENDIENTE
        # Guardar en base de datos con reintentos
        try:
            guardado_exitoso = await guardar_folio_con_reintentos(datos, message.from_user.id, message.from_user.username)
            
            if not guardado_exitoso:
                await message.answer(
                    f"❌ ERROR CRÍTICO\n\n"
                    f"No se pudo guardar el folio después de múltiples intentos.\n"
                    f"Por favor, intente nuevamente con /permiso"
                )
                return

            # También guardar en borradores
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
                print(f"Error guardando en borradores (no crítico): {e}")

            # INICIAR TIMER DE ELIMINACIÓN AUTOMÁTICA (2 HORAS)
            await iniciar_timer_eliminacion(message.from_user.id, datos['folio'])

            # Mensaje de instrucciones de pago elegante
            await message.answer(
                f"💰 INSTRUCCIONES PARA LIQUIDACIÓN DEL SERVICIO\n\n"
                f"Estimado usuario, a continuación le proporcionamos los detalles para completar su trámite:\n\n"
                f"📄 Folio de referencia: {datos['folio']}\n"
                f"💵 Monto a liquidar: 250 pesos\n"
                f"⏰ Tiempo disponible para el pago: 2 horas exactas\n\n"
                
                "🏦 MODALIDAD 1 - TRANSFERENCIA BANCARIA ELECTRÓNICA:\n"
                "• Institución financiera: SPIN BY OXXO\n"
                "• Cuenta beneficiaria: GUILLERMO S.R\n"
                "• Número de cuenta: 728969000048442454\n"
                "• Concepto de pago: Permiso " + datos['folio'] + "\n\n"
                
                "🏪 MODALIDAD 2 - DEPÓSITO EN ESTABLECIMIENTO OXXO:\n"
                "• Referencia de pago: 2242170180214090\n"
                "• Tarjeta SPIN autorizada\n"
                "• Titular de la cuenta: GUILLERMO S.R\n"
                "• Cantidad exacta: 250 pesos\n\n"
                
                f"📸 PROCEDIMIENTO FINAL: Una vez efectuado el pago, le solicitamos muy cordialmente enviar la fotografía nítida de su comprobante para la validación correspondiente por parte de nuestro equipo técnico.\n\n"
                f"⚠️ ADVERTENCIA IMPORTANTE: Le recordamos respetuosamente que si no completa el proceso de pago dentro de las próximas 2 horas, el folio {datos['folio']} será eliminado automáticamente de nuestro sistema según nuestras políticas establecidas."
            )
            
        except Exception as e:
            print(f"Error guardando en Supabase: {e}")
            await message.answer(f"⚠️ ADVERTENCIA DEL SISTEMA: La documentación ha sido generada exitosamente, sin embargo se presentó un inconveniente menor en el registro: {str(e)}\n\nSi requiere asistencia, mencione este folio: {datos['folio']}")
        
    except Exception as e:
        await message.answer(
            f"❌ ERROR TÉCNICO EN EL SISTEMA\n\n"
            f"Estimado usuario, lamentamos informarle que se ha presentado un inconveniente técnico durante el procesamiento: {str(e)}\n\n"
            f"Le solicitamos muy amablemente intentar nuevamente con el comando /permiso\n"
            f"Si el problema persiste, le rogamos contactar a nuestro equipo de soporte técnico."
        )
        print(f"Error: {e}")
    finally:
        await state.clear()

# ------------ CÓDIGO SECRETO ADMIN MEJORADO ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    
    # Verificar formato: SERO + número de folio
    if len(texto) > 4:
        folio_admin = texto[4:]  # Quitar "SERO" del inicio
        
        # Buscar si hay un timer activo con ese folio
        folio_encontrado = False
        user_con_folio = None
        
        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
            folio_encontrado = True
        
        if folio_encontrado:
            # Cancelar timer
            cancelar_timer_folio(folio_admin)
            
            # Actualizar estado en base de datos
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
                f"✅ VALIDACIÓN ADMINISTRATIVA EJECUTADA EXITOSAMENTE\n\n"
                f"🔐 Código administrativo procesado correctamente\n"
                f"📄 Folio intervenido: {folio_admin}\n"
                f"⏰ Timer de eliminación cancelado exitosamente\n"
                f"📊 Estado actualizado a: VALIDADO_ADMIN\n"
                f"👤 Usuario beneficiado: {user_con_folio}\n\n"
                f"El ciudadano ha sido notificado automáticamente de la validación."
            )
            
            # Notificar al usuario que su permiso está validado
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - ESTADO DE JALISCO\n\n"
                    f"Estimado usuario, nos complace informarle que su trámite ha sido validado exitosamente por nuestro equipo administrativo.\n\n"
                    f"📄 Folio de referencia: {folio_admin}\n"
                    f"✅ Estado actual: COMPLETAMENTE VALIDADO\n"
                    f"🚗 Su permiso cuenta con plena validez para circular\n\n"
                    f"Agradecemos su confianza en el Sistema Digital del Estado de Jalisco.\n"
                    f"Quedamos a su disposición para cualquier consulta adicional."
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            await message.answer(
                f"❌ FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n\n"
                f"📄 Folio consultado: {folio_admin}\n"
                f"⚠️ No se encontró ningún proceso activo para este folio.\n\n"
                f"Posibles escenarios:\n"
                f"• El timer ya expiró automáticamente\n"
                f"• El usuario ya envió comprobante de pago\n"
                f"• El folio no existe o es incorrecto\n"
                f"• El folio fue validado previamente\n\n"
                f"Favor de verificar el número de folio y su estado actual."
            )
    else:
        await message.answer(
            "⚠️ FORMATO DE CÓDIGO ADMINISTRATIVO INCORRECTO\n\n"
            "El formato correcto es: SERO[número_de_folio]\n\n"
            "Ejemplo de uso: SERO5908167415\n\n"
            "Le solicitamos verificar el formato y reintentar."
        )

# Handler para recibir comprobantes de pago (imágenes)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            frases_sin_folios = [
                "ℹ️ NO HAY TRÁMITES PENDIENTES DE PAGO\n\n"
                "Estimado usuario, en este momento no se localizan permisos pendientes de liquidación asociados a su cuenta.\n\n"
                "Si desea tramitar un nuevo permiso de circulación, le invitamos cordialmente a utilizar el comando /permiso para iniciar el proceso.",
                
                "📄 SIN EXPEDIENTES ACTIVOS\n\n"
                "Distinguido ciudadano, no se encontraron folios pendientes de validación de pago en nuestro sistema.\n\n"
                "Para iniciar un nuevo trámite vehicular, sírvase utilizar: /permiso",
                
                "🔍 NO HAY FOLIOS EN PROCESO DE PAGO\n\n"
                "Estimado usuario, no se localizaron permisos esperando comprobante de pago.\n\n"
                "Comando para nuevo permiso: /permiso"
            ]
            await message.answer(random.choice(frases_sin_folios))
            return
        
        # Si tiene varios folios, preguntar cuál
        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 MÚLTIPLES EXPEDIENTES EN PROCESO\n\n"
                f"Estimado usuario, detectamos que tiene {len(folios_usuario)} folios pendientes de pago:\n\n"
                f"{lista_folios}\n\n"
                f"Para procesar correctamente su comprobante, le solicitamos muy cordialmente especificar el NÚMERO DE FOLIO exacto al que corresponde el pago que acaba de enviar.\n\n"
                f"Ejemplo de respuesta: {folios_usuario[0]}"
            )
            return
        
        # Solo un folio activo, procesar automáticamente
        folio = folios_usuario[0]
        
        # Cancelar timer de eliminación
        cancelar_timer_folio(folio)
        
        # Actualizar estado en base de datos
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            
            frases_comprobante_recibido = [
                f"✅ COMPROBANTE RECIBIDO EXITOSAMENTE\n\n"
                f"Estimado usuario, nos complace informarle que su comprobante de pago ha sido recibido y registrado correctamente en nuestro sistema.\n\n"
                f"📄 Folio de referencia: {folio}\n"
                f"📸 Estado del comprobante: Recibido y en proceso de verificación\n"
                f"⏰ Cronómetro de eliminación: Detenido exitosamente\n\n"
                f"🔍 Su documentación está siendo revisada por nuestro equipo especializado de validación. Una vez confirmado el pago, su permiso quedará completamente activo para su uso.\n\n"
                f"Agradecemos profundamente su confianza en el Sistema Digital del Estado de Jalisco.",
                
                f"💾 COMPROBANTE REGISTRADO EN EL SISTEMA\n\n"
                f"Distinguido ciudadano, su comprobante ha sido almacenado satisfactoriamente en nuestra base de datos gubernamental.\n\n"
                f"📋 Número de expediente: {folio}\n"
                f"📷 Imagen del comprobante: Registrada correctamente\n"
                f"🛑 Proceso de eliminación automática: Cancelado\n\n"
                f"⚡ El proceso de validación ha sido iniciado automáticamente por nuestros sistemas. Su permiso será activado una vez que nuestro equipo confirme la transacción.\n\n"
                f"Le expresamos nuestro sincero agradecimiento por utilizar nuestros servicios digitales."
            ]
            await message.answer(random.choice(frases_comprobante_recibido))
            
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ COMPROBANTE RECIBIDO\n\n"
                f"📄 Folio: {folio}\n"
                f"📸 Su comprobante fue recibido exitosamente y el cronómetro se detuvo.\n\n"
                f"⚠️ Se presentó un inconveniente menor actualizando el estado en el sistema, sin embargo su comprobante está correctamente guardado.\n\n"
                f"Si requiere asistencia adicional, sírvase mencionar este folio: {folio}"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO COMPROBANTE\n\n"
            "Estimado usuario, se ha presentado un inconveniente técnico al procesar la imagen de su comprobante.\n\n"
            "Le solicitamos muy amablemente intentar enviar nuevamente la fotografía nítida de su comprobante de pago.\n\n"
            "Si el problema persiste, le rogamos contactar a nuestro equipo de soporte técnico."
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
                f"❌ FOLIO NO LOCALIZADO EN SUS EXPEDIENTES ACTIVOS\n\n"
                f"Estimado usuario, el folio '{folio_especificado}' no se encuentra entre sus trámites pendientes de pago.\n\n"
                f"Sus folios activos registrados son:\n" + 
                '\n'.join([f"• {f}" for f in folios_usuario]) +
                f"\n\nLe solicitamos verificar la información e ingresar un folio válido de la lista anterior:"
            )
            return
        
        # Folio válido - cancelar timer
        cancelar_timer_folio(folio_especificado)
        
        # Limpiar estado pending
        del pending_comprobantes[user_id]
        
        # Actualizar en base de datos
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
                f"✅ FOLIO CONFIRMADO Y COMPROBANTE ASOCIADO\n\n"
                f"Distinguido usuario, la asociación ha sido completada exitosamente:\n\n"
                f"📄 Folio validado: {folio_especificado}\n"
                f"📸 Comprobante: Correctamente vinculado al expediente\n"
                f"⏰ Cronómetro de eliminación: Detenido satisfactoriamente\n\n"
                f"🔍 Su comprobante está siendo procesado por nuestro equipo de verificación. Una vez validado el pago, su permiso quedará completamente activo.\n\n"
                f"Agradecemos su colaboración y paciencia durante el proceso."
            )
            
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ FOLIO CONFIRMADO\n\n"
                f"📄 Folio: {folio_especificado}\n"
                f"⏰ Cronómetro detenido exitosamente\n\n"
                f"Su comprobante ha sido asociado al folio correctamente.\n"
                f"El equipo de validación procesará su pago a la brevedad."
            )
            
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer("❌ Error procesando el folio especificado. Le solicitamos intentar nuevamente.")

# Comando para ver folios activos
@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            frases_sin_folios = [
                "ℹ️ NO HAY EXPEDIENTES ACTIVOS\n\n"
                "Estimado usuario, en este momento no tiene folios pendientes de pago registrados en nuestro sistema.\n\n"
                "Para tramitar un nuevo permiso, le invitamos a utilizar el comando /permiso",
                
                "📄 SIN TRÁMITES VIGENTES\n\n"
                "Distinguido ciudadano, no se encontraron expedientes activos asociados a su cuenta.\n\n"
                "Comando para iniciar nuevo permiso: /permiso",
                
                "🔍 ESTADO: SIN FOLIOS PENDIENTES\n\n"
                "Estimado usuario, actualmente no tiene permisos esperando liquidación.\n\n"
                "Para nuevo trámite: /permiso"
            ]
            await message.answer(random.choice(frases_sin_folios))
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_transcurrido = int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, 120 - tiempo_transcurrido)
                lista_folios.append(f"• {folio} ({tiempo_restante} minutos restantes)")
            else:
                lista_folios.append(f"• {folio} (cronómetro detenido)")
        
        await message.answer(
            f"📋 SUS EXPEDIENTES ACTIVOS ({len(folios_usuario)})\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio mantiene su cronómetro independiente de 2 horas.\n"
            f"📸 Para enviar comprobante de pago, utilice una imagen.\n"
            f"💰 Inversión por permiso: Según tarifa oficial vigente"
        )
        
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer("❌ Error consultando expedientes activos. Intente nuevamente.")

# Handler para preguntas sobre costo/precio
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    try:
        frases_costo = [
            "💰 INFORMACIÓN SOBRE LA INVERSIÓN DEL SERVICIO\n\n"
            "Estimado usuario, el costo del permiso de circulación corresponde a la tarifa oficial establecida por el Estado de Jalisco.\n\n"
            "📋 Vigencia del documento: 30 días calendario\n"
            "💳 Modalidades de pago: Transferencia bancaria y establecimientos OXXO\n\n"
            "Para iniciar su trámite, le invitamos cordialmente a utilizar /permiso",
            
            "💵 TARIFA GUBERNAMENTAL OFICIAL - JALISCO\n\n"
            "Distinguido ciudadano, la inversión requerida corresponde a las tarifas vigentes del gobierno estatal.\n\n"
            "⏰ Período de validez: 30 días naturales\n"
            "🏪 Puntos de pago autorizados: Red OXXO y transferencias\n\n"
            "Comando de inicio: /permiso"
        ]
        await message.answer(random.choice(frases_costo))
    except Exception as e:
        print(f"[ERROR] responder_costo: {e}")
        await message.answer("💰 Inversión según tarifa oficial. Use /permiso para tramitar.")

@dp.message()
async def fallback(message: types.Message):
    respuestas_elegantes = [
        "🏛️ Sistema Digital del Estado de Jalisco. Para tramitar su permiso de circulación utilice /permiso",
        "📋 Plataforma gubernamental de servicios digitales. Comando disponible: /permiso para iniciar trámite",
        "⚡ Sistema oficial en línea. Use /permiso para generar su documentación gubernamental",
        "🚗 Servicio de permisos vehiculares de Jalisco. Inicie su proceso con /permiso",
        "💰 Inversión según tarifa oficial. Vigencia: 30 días. Comando: /permiso",
        "🎯 Sistema automatizado estatal. Para permisos de circulación: /permiso"
    ]
    await message.answer(random.choice(respuestas_elegantes))

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
        print("[CONTINUIDAD] Folios se reanudarán desde el último en Supabase")
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
            "timer_eliminacion": "2 horas",
            "active_timers": len(timers_activos),
            "continuidad_folios": "Habilitada desde Supabase"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/status")
async def status_detail():
    """Endpoint de diagnóstico detallado"""
    try:
        return {
            "sistema": "Jalisco Digital v2.0 - Continuidad de Folios",
            "entidad": "Jalisco",
            "vigencia_dias": 30,
            "tiempo_eliminacion": "2 horas con recordatorios",
            "total_timers_activos": len(timers_activos),
            "folios_con_timer": list(timers_activos.keys()),
            "usuarios_con_folios": len(user_folios),
            "continuidad": "Folios continúan desde último en BD",
            "detalle_usuarios": {str(uid): folios for uid, folios in user_folios.items()},
            "timestamp": datetime.now().isoformat(),
            "status": "Operacional con continuidad garantizada"
        }
    except Exception as e:
        return {"error": str(e), "status": "Error"}

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] Continuidad de folios desde Supabase habilitada")
        print(f"[CONFIG] Entidad: Jalisco - Vigencia: 30 días - Auto-eliminación: 2 horas")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
