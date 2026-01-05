from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, Response
from io import BytesIO
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeVideo, User, Chat, Channel
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.functions.upload import GetFileRequest
import asyncio
import os
import json
import secrets
from werkzeug.utils import secure_filename
import time
from datetime import datetime
import threading
import pymysql
from contextlib import contextmanager

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 * 1024  # 5GB max (para videos grandes de hasta 4GB)
# Configuración de sesiones para que funcionen correctamente
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False  # Cambiar a True en producción con HTTPS
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 horas

# Crear carpetas necesarias
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('sessions', exist_ok=True)
# NO crear video_cache_temp - todo se sirve directamente desde la nube de Telegram

# Helper para calcular limit válido para GetFileRequest
# Telegram requiere que limit sea múltiplo de 1024 y máximo 1MB
def get_valid_limit(requested_size, max_allowed=None):
    """
    Calcula un limit válido para GetFileRequest.
    Telegram requiere que limit sea múltiplo de 1024 y máximo 1MB.
    
    Args:
        requested_size: El tamaño solicitado en bytes
        max_allowed: Tamaño máximo permitido (opcional). Si se proporciona y es menor que 1024,
                     se permite usar ese tamaño exacto incluso si no es múltiplo de 1024.
    """
    # Validar que requested_size sea un número válido
    if not isinstance(requested_size, (int, float)) or requested_size <= 0:
        requested_size = 1024  # Valor por defecto seguro
    
    max_limit = 1024 * 1024  # 1MB máximo (1048576 bytes)
    
    # Si max_allowed está definido y es menor que 1024, permitir usar ese tamaño exacto
    if max_allowed is not None and max_allowed < 1024 and max_allowed > 0:
        # Telegram permite limit menor que 1024 si es el tamaño restante exacto
        return int(max_allowed)
    
    min_limit = 1024  # Mínimo válido (solo si no hay max_allowed < 1024)
    
    # Redondear hacia arriba al múltiplo de 1024 más cercano
    # Usar floor division para asegurar que siempre sea múltiplo de 1024
    valid_limit = ((int(requested_size) + 1023) // 1024) * 1024
    
    # Asegurar que no exceda el máximo global (1MB)
    valid_limit = min(valid_limit, max_limit)
    
    # Si max_allowed está definido, asegurar que no lo exceda
    if max_allowed is not None:
        valid_limit = min(valid_limit, max_allowed)
        # Si después de limitar a max_allowed, el valor es menor que 1024 pero max_allowed >= 1024,
        # redondear hacia abajo al múltiplo de 1024 más cercano
        if valid_limit < 1024 and max_allowed >= 1024:
            valid_limit = (max_allowed // 1024) * 1024
            if valid_limit < 1024:
                valid_limit = 1024
    else:
        # Asegurar que sea al menos el mínimo válido (solo si no hay max_allowed)
        valid_limit = max(valid_limit, min_limit)
    
    # Verificación final: asegurar que sea múltiplo de 1024 (excepto si max_allowed < 1024)
    if max_allowed is None or max_allowed >= 1024:
        if valid_limit % 1024 != 0:
            valid_limit = ((valid_limit // 1024) + 1) * 1024
            if max_allowed is not None:
                valid_limit = min(valid_limit, max_allowed)
            valid_limit = min(valid_limit, max_limit)
    
    return int(valid_limit)

# Función para limpiar archivos antiguos de uploads (más de 1 hora)
def cleanup_old_uploads():
    """Eliminar archivos temporales antiguos de la carpeta uploads"""
    try:
        upload_folder = app.config['UPLOAD_FOLDER']
        if not os.path.exists(upload_folder):
            return
        
        current_time = time.time()
        deleted_count = 0
        
        for filename in os.listdir(upload_folder):
            file_path = os.path.join(upload_folder, filename)
            if os.path.isfile(file_path):
                # Obtener el tiempo de modificación del archivo
                file_mtime = os.path.getmtime(file_path)
                # Si el archivo tiene más de 1 hora, eliminarlo
                if current_time - file_mtime > 3600:  # 1 hora = 3600 segundos
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                        print(f"🗑️ Archivo antiguo eliminado: {filename}")
                    except Exception as e:
                        print(f"⚠️ Error eliminando archivo antiguo {filename}: {e}")
        
        if deleted_count > 0:
            print(f"✅ Limpieza completada: {deleted_count} archivo(s) antiguo(s) eliminado(s)")
    except Exception as e:
        print(f"⚠️ Error en limpieza de archivos antiguos: {e}")

# Limpiar archivos antiguos al iniciar
cleanup_old_uploads()

# Almacenar clientes de Telegram por sesión
telegram_clients = {}
# Lock para prevenir creación concurrente de clientes con la misma sesión SQLite
_client_creation_lock = threading.Lock()
upload_progress = {}  # Almacenar progreso de subidas
video_memory_cache = {}  # Caché en memoria de videos (como Telegram - pre-cargados)
CONFIG_FILE = 'telegram_config.json'  # Archivo para guardar configuración
DB_CONFIG_FILE = 'db_config.json'  # Archivo de configuración de MySQL

# Cargar configuración de base de datos
def load_db_config():
    """Cargar configuración de MySQL desde archivo"""
    try:
        if os.path.exists(DB_CONFIG_FILE):
            with open(DB_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            print("⚠️ db_config.json no encontrado. Usando valores por defecto.")
            return {
                'host': 'localhost',
                'user': 'telegram_user',
                'password': '',
                'database': 'telegram_app',
                'charset': 'utf8mb4'
            }
    except Exception as e:
        print(f"⚠️ Error cargando configuración de DB: {e}")
        return None

db_config = load_db_config()

# Función para obtener conexión a MySQL
@contextmanager
def get_db_connection():
    """Context manager para obtener conexión a MySQL"""
    if not db_config:
        error_msg = "Configuración de base de datos no disponible. Verifica que db_config.json exista o que la configuración por defecto sea correcta."
        print(f"❌ {error_msg}")
        raise Exception(error_msg)
    
    conn = None
    try:
        print(f"🔌 Intentando conectar a MySQL: host={db_config['host']}, database={db_config['database']}, user={db_config['user']}")
        conn = pymysql.connect(
            host=db_config['host'],
            user=db_config['user'],
            password=db_config['password'],
            database=db_config['database'],
            charset=db_config.get('charset', 'utf8mb4'),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10
        )
        print(f"✅ Conexión a MySQL establecida exitosamente")
        yield conn
    except pymysql.Error as db_error:
        error_type = type(db_error).__name__
        error_code = getattr(db_error, 'args', [None])[0] if hasattr(db_error, 'args') and db_error.args else None
        error_msg = str(db_error)
        print(f"❌ Error de MySQL: {error_type} (código: {error_code}): {error_msg}")
        print(f"📋 Configuración usada: host={db_config.get('host')}, database={db_config.get('database')}, user={db_config.get('user')}")
        import traceback
        print(traceback.format_exc())
        raise Exception(f"Error de conexión a MySQL: {error_msg}") from db_error
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        print(f"❌ Error de conexión a MySQL: {error_type}: {error_msg}")
        import traceback
        print(traceback.format_exc())
        raise Exception(f"Error de conexión a MySQL: {error_msg}") from e
    finally:
        if conn:
            try:
                conn.close()
                print(f"🔌 Conexión a MySQL cerrada")
            except:
                pass

# Funciones para trabajar con videos en MySQL
def get_video_from_db(video_id):
    """Obtener información de un video desde MySQL"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM videos WHERE video_id = %s",
                    (video_id,)
                )
                result = cursor.fetchone()
                if result:
                    return {
                        'message_id': result['message_id'],
                        'chat_id': result['chat_id'],
                        'filename': result['filename'],
                        'timestamp': result['timestamp'].timestamp() if isinstance(result['timestamp'], datetime) else result['timestamp'],
                        'file_size': result.get('file_size'),
                        'phone': None  # No almacenamos phone en la tabla, se obtiene de otra forma
                    }
                return None
    except pymysql.Error as db_error:
        error_type = type(db_error).__name__
        error_msg = str(db_error)
        print(f"❌ Error de MySQL obteniendo video {video_id}: {error_type}: {error_msg}")
        import traceback
        print(traceback.format_exc())
        # Relanzar la excepción para que el llamador pueda manejarla
        raise Exception(f"Error de base de datos MySQL: {error_msg}") from db_error
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        print(f"❌ Error obteniendo video desde DB: {error_type}: {error_msg}")
        import traceback
        print(traceback.format_exc())
        # Relanzar la excepción para que el llamador pueda manejarla
        raise

def find_video_by_message(chat_id, message_id, phone):
    """Buscar video existente por chat_id, message_id y phone"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Buscar por chat_id y message_id (phone no está en la tabla, se maneja en la app)
                chat_id_str = str(chat_id) if chat_id != 'me' else 'me'
                cursor.execute(
                    "SELECT video_id FROM videos WHERE chat_id = %s AND message_id = %s ORDER BY created_at DESC LIMIT 1",
                    (chat_id_str, message_id)
                )
                result = cursor.fetchone()
                if result:
                    video_id = result['video_id']
                    print(f"🔍 Video encontrado: chat_id={chat_id_str}, message_id={message_id}, video_id={video_id}")
                    return video_id
                else:
                    print(f"🔍 Video NO encontrado: chat_id={chat_id_str}, message_id={message_id}")
                return None
    except Exception as e:
        print(f"❌ Error buscando video en DB: {e}")
        import traceback
        print(traceback.format_exc())
        return None

def save_video_to_db(video_id, chat_id, message_id, filename, timestamp, file_size=None):
    """Guardar o actualizar video en MySQL"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Verificar si existe
                cursor.execute("SELECT video_id FROM videos WHERE video_id = %s", (video_id,))
                exists = cursor.fetchone()
                
                if exists:
                    # Actualizar
                    cursor.execute(
                        """UPDATE videos SET chat_id = %s, message_id = %s, filename = %s, 
                           timestamp = FROM_UNIXTIME(%s), file_size = %s, updated_at = NOW() 
                           WHERE video_id = %s""",
                        (str(chat_id), message_id, filename, timestamp, file_size, video_id)
                    )
                else:
                    # Insertar nuevo
                    cursor.execute(
                        """INSERT INTO videos (video_id, chat_id, message_id, filename, timestamp, file_size) 
                           VALUES (%s, %s, %s, %s, FROM_UNIXTIME(%s), %s)""",
                        (video_id, str(chat_id), message_id, filename, timestamp, file_size)
                    )
                conn.commit()
                return True
    except Exception as e:
        print(f"❌ Error guardando video en DB: {e}")
        return False

def get_all_videos_from_db():
    """Obtener todos los videos desde MySQL"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM videos ORDER BY timestamp DESC")
                results = cursor.fetchall()
                videos = {}
                for row in results:
                    video_id = row['video_id']
                    videos[video_id] = {
                        'message_id': row['message_id'],
                        'chat_id': row['chat_id'],
                        'filename': row['filename'],
                        'timestamp': row['timestamp'].timestamp() if isinstance(row['timestamp'], datetime) else row['timestamp'],
                        'file_size': row.get('file_size')
                    }
                return videos
    except Exception as e:
        print(f"❌ Error obteniendo todos los videos desde DB: {e}")
        return {}

# Verificar conexión a MySQL al iniciar
if db_config:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) as count FROM videos")
                result = cursor.fetchone()
                print(f"✅ Conexión a MySQL exitosa. Videos en DB: {result['count']}")
    except Exception as e:
        print(f"⚠️ Advertencia: No se pudo conectar a MySQL: {e}")
        print("⚠️ La aplicación continuará pero algunas funciones pueden no funcionar correctamente.")

# Almacenar loops por thread
_thread_loops = {}
_thread_lock = threading.Lock()

def get_event_loop():
    """Obtener o crear un event loop para el thread actual"""
    thread_id = threading.current_thread().ident
    
    with _thread_lock:
        if thread_id in _thread_loops:
            loop = _thread_loops[thread_id]
            if loop.is_closed():
                del _thread_loops[thread_id]
            else:
                # CRÍTICO: Asegurarse de que el loop esté establecido como el loop actual del thread
                # Esto es necesario porque Flask puede reutilizar threads y el loop puede no estar configurado
                try:
                    asyncio.set_event_loop(loop)
                except RuntimeError:
                    # Si hay un error, crear un nuevo loop
                    pass
                else:
                    return loop
        
        # Crear nuevo loop para este thread
        loop = asyncio.new_event_loop()
        # CRÍTICO: Establecer el loop como el loop actual del thread ANTES de guardarlo
        asyncio.set_event_loop(loop)
        _thread_loops[thread_id] = loop
        return loop

def run_async(coro, loop=None, timeout=None):
    """Ejecutar una corrutina en un event loop para el thread actual
    
    Args:
        coro: Corrutina a ejecutar
        loop: Event loop a usar (opcional)
        timeout: Timeout en segundos (opcional, por defecto 300 para operaciones largas)
    """
    if timeout is None:
        timeout = 300  # 5 minutos por defecto para operaciones como subir videos
    
    if loop is None:
        loop = get_event_loop()
    
    # Asegurarse de que el loop no esté cerrado
    if loop.is_closed():
        # Si el loop está cerrado, crear uno nuevo
        thread_id = threading.current_thread().ident
        with _thread_lock:
            if thread_id in _thread_loops:
                del _thread_loops[thread_id]
        loop = get_event_loop()
    
    # CRÍTICO: Establecer el loop como el loop actual del thread SIEMPRE
    # Esto previene el error "no current event loop" cuando Flask reutiliza threads
    try:
        current_loop = asyncio.get_event_loop()
        # Si el loop actual es diferente o está cerrado, establecer el nuevo loop
        if current_loop != loop or current_loop.is_closed():
            asyncio.set_event_loop(loop)
    except RuntimeError:
        # No hay loop actual, establecer este
        asyncio.set_event_loop(loop)
    
    try:
        # Verificar si el loop ya está corriendo
        if loop.is_running():
            # Si el loop está corriendo, usar run_coroutine_threadsafe para ejecutar en ese loop
            # NO crear un nuevo loop porque el cliente detectaría el cambio
            import concurrent.futures
            
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                result = future.result(timeout=timeout)
                return result
            except concurrent.futures.TimeoutError:
                future.cancel()
                raise asyncio.TimeoutError(f"Operación excedió el timeout de {timeout} segundos")
        else:
            # Si el loop no está corriendo, usar run_until_complete normalmente
            return loop.run_until_complete(coro)
    except Exception as e:
        # Re-raise la excepción para que se maneje arriba
        raise
    finally:
        # NO cerrar el loop - mantenerlo vivo para Telethon
        # Asegurarse de que el loop sigue siendo el loop actual
        try:
            asyncio.set_event_loop(loop)
        except:
            pass

def load_saved_config():
    """Cargar configuración guardada"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
            return None
    return None

def save_config(api_id, api_hash, phone, session_name):
    """Guardar configuración"""
    config = {
        'api_id': api_id,
        'api_hash': api_hash,
        'phone': phone,
        'session_name': session_name
    }
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f)
        return True
    except:
        return False

def delete_config():
    """Eliminar configuración guardada"""
    try:
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
        return True
    except:
        return False

@app.route('/')
def index():
    """Página principal - configuración inicial"""
    # Solo verificar si hay una sesión activa válida para ESTE usuario específico
    # NO cargar configuración guardada globalmente - cada usuario debe iniciar sesión
    if 'phone' in session:
        # Verificar si la sesión es válida para este usuario
        session_name = session.get('session_name', '')
        if session_name and os.path.exists(session_name + '.session'):
            # Verificar que el archivo de sesión pertenece a este usuario
            phone = session.get('phone')
            if phone and session_name == f"sessions/{secure_filename(phone)}":
                return redirect(url_for('home'))
            else:
                # Sesión no coincide, limpiar
                session.clear()
    
    # NO cargar configuración guardada globalmente
    # Cada usuario debe iniciar sesión con su propia cuenta
    return render_template('index.html')

@app.route('/api/configure', methods=['POST'])
def configure():
    """Configurar API de Telegram"""
    data = request.json
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    phone = data.get('phone')
    
    if not all([api_id, api_hash, phone]):
        return jsonify({'error': 'Faltan datos requeridos'}), 400
    
    session_name = f"sessions/{secure_filename(phone)}"
    
    # Guardar en sesión
    session['api_id'] = api_id
    session['api_hash'] = api_hash
    session['phone'] = phone
    session['session_name'] = session_name
    
    # NO guardar en archivo global - cada usuario tiene su propia sesión
    # La sesión de Flask ya maneja la persistencia por usuario mediante cookies
    
    return jsonify({'message': 'Configuración guardada', 'next': 'connect'})

@app.route('/api/connect', methods=['POST'])
def connect():
    """Conectar a Telegram"""
    print("🔌 Intentando conectar a Telegram...")
    if 'api_id' not in session:
        return jsonify({'error': 'Primero debes configurar la API'}), 400
    
    api_id = int(session['api_id'])
    api_hash = session['api_hash']
    phone = session['phone']
    session_name = session['session_name']
    
    print(f"📱 Datos: API ID={api_id}, Phone={phone}, Session={session_name}")
    
    # Verificar si ya hay un cliente conectado esperando código
    if phone in telegram_clients and telegram_clients[phone].get('needs_code', False):
        print("ℹ️ Ya hay un código pendiente, retornando...")
        return jsonify({'message': 'Código enviado', 'needs_code': True})
    
    try:
        # CRÍTICO: Cerrar cualquier cliente existente para esta sesión antes de crear uno nuevo
        # Esto previene el error "database is locked"
        if phone in telegram_clients:
            old_client_data = telegram_clients[phone]
            old_client = old_client_data.get('client')
            if old_client:
                try:
                    print("🔒 Cerrando cliente existente antes de crear uno nuevo...")
                    old_loop = old_client_data.get('loop')
                    if old_loop and not old_loop.is_closed():
                        if old_client.is_connected():
                            run_async(old_client.disconnect(), old_loop, timeout=5)
                    del telegram_clients[phone]
                    time.sleep(0.2)  # Esperar a que SQLite libere el lock
                    print("✅ Cliente antiguo cerrado")
                except Exception as e:
                    print(f"⚠️ Error cerrando cliente antiguo: {e}")
        
        # Asegurarse de que hay un event loop antes de crear el cliente
        print("🔄 Creando event loop...")
        loop = get_event_loop()
        
        # Crear cliente de Telegram (necesita un loop disponible)
        # Usar lock para prevenir creación concurrente
        print("📱 Creando cliente de Telegram...")
        with _client_creation_lock:
            client = TelegramClient(session_name, api_id, api_hash, loop=loop, timeout=10)
        
        # Conectar usando el event loop del thread actual
        print("🔌 Conectando a Telegram...")
        async def connect_and_check():
            try:
                # Conectar sin timeout muy corto para evitar problemas
                if not client.is_connected():
                    await client.connect()
                print("✅ Conectado, verificando autorización...")
                is_auth = await client.is_user_authorized()
                return is_auth
            except Exception as e:
                print(f"❌ Error en connect_and_check: {e}")
                raise
        
        is_authorized = run_async(connect_and_check(), loop)
        print(f"🔐 Autorizado: {is_authorized}")
        
        if not is_authorized:
            # Enviar código de verificación
            print("📨 Enviando código de verificación...")
            async def send_code():
                try:
                    result = await client.send_code_request(phone)
                    print("✅ Código enviado exitosamente")
                    return result
                except Exception as e:
                    print(f"❌ Error al enviar código: {e}")
                    raise
            
            code_result = run_async(send_code(), loop)
            # Obtener el phone_code_hash del resultado
            phone_code_hash = None
            if hasattr(code_result, 'phone_code_hash'):
                phone_code_hash = code_result.phone_code_hash
            elif isinstance(code_result, dict) and 'phone_code_hash' in code_result:
                phone_code_hash = code_result['phone_code_hash']
            
            print(f"📝 Phone code hash guardado: {phone_code_hash[:10] if phone_code_hash else 'None'}...")
            
            # Guardar el cliente y mantenerlo conectado
            telegram_clients[session['phone']] = {
                'client': client,
                'api_id': api_id,
                'api_hash': api_hash,
                'session_name': session_name,
                'needs_code': True,
                'phone_code_hash': phone_code_hash,
                'loop': loop  # Guardar también el loop
            }
            print("✅ Retornando respuesta: código enviado")
            # NO desconectar el cliente - mantenerlo conectado
            return jsonify({'message': 'Código enviado', 'needs_code': True})
        else:
            # Ya está autorizado
            print("✅ Ya está autorizado")
            telegram_clients[session['phone']] = {
                'client': client,
                'api_id': api_id,
                'api_hash': api_hash,
                'session_name': session_name,
                'needs_code': False,
                'loop': loop  # Guardar el loop
            }
            return jsonify({'message': 'Conectado exitosamente', 'connected': True})
            
    except asyncio.TimeoutError:
        print("❌ Timeout al conectar")
        return jsonify({'error': 'Tiempo de espera agotado. Verifica tu conexión a internet.'}), 500
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"❌ Error: {error_msg}")
        print(traceback.format_exc())
        # Simplificar el mensaje de error para el usuario
        if 'timeout' in error_msg.lower():
            error_msg = 'Tiempo de espera agotado. Intenta de nuevo.'
        return jsonify({'error': error_msg}), 500

@app.route('/api/verify_code', methods=['POST'])
def verify_code():
    """Verificar código de Telegram"""
    data = request.json
    code = data.get('code')
    password = data.get('password')  # Para 2FA si está activado
    
    if 'phone' not in session or session['phone'] not in telegram_clients:
        return jsonify({'error': 'No hay conexión pendiente'}), 400
    
    phone = session['phone']
    client_data = telegram_clients[phone]
    
    # Verificar que tenemos el phone_code_hash
    phone_code_hash = client_data.get('phone_code_hash')
    if not phone_code_hash:
        return jsonify({'error': 'No se encontró el hash del código. Por favor, intenta conectar de nuevo.'}), 400
    
    # Usar el cliente existente si está disponible, o recrearlo
    if 'client' in client_data and client_data['client'] is not None:
        client = client_data['client']
        # Obtener el loop del cliente o crear uno nuevo
        loop = client_data.get('loop')
        if not loop or loop.is_closed():
            loop = get_event_loop()
            client_data['loop'] = loop
        print("🔄 Usando cliente existente...")
    else:
        # Asegurarse de que hay un event loop antes de crear el cliente
        loop = get_event_loop()
        # Recrear cliente en este thread con lock para prevenir "database is locked"
        with _client_creation_lock:
            # Cerrar cliente antiguo si existe
            if 'client' in client_data and client_data['client']:
                try:
                    old_client = client_data['client']
                    if old_client.is_connected():
                        old_loop = client_data.get('loop')
                        if old_loop and not old_loop.is_closed():
                            run_async(old_client.disconnect(), old_loop, timeout=5)
                    time.sleep(0.1)  # Esperar a que SQLite libere el lock
                except:
                    pass
            
            client = TelegramClient(client_data['session_name'], client_data['api_id'], client_data['api_hash'], loop=loop)
            client_data['loop'] = loop
            print("🆕 Creando nuevo cliente...")
    
    try:
        # Asegurarse de que el cliente esté conectado
        async def verify_and_sign_in():
            if not client.is_connected():
                print("🔌 Conectando cliente...")
                await client.connect()
            
            if not await client.is_user_authorized():
                # Usar el phone_code_hash guardado
                print(f"🔐 Verificando código con hash: {phone_code_hash[:10]}...")
                try:
                    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    print("✅ Código verificado exitosamente")
                except SessionPasswordNeededError:
                    if not password:
                        raise Exception("Se requiere contraseña 2FA")
                    await client.sign_in(password=password)
                    print("✅ Contraseña 2FA verificada")
            else:
                print("✅ Ya está autorizado")
        
        run_async(verify_and_sign_in(), loop)
        
        # Actualizar el cliente en el diccionario
        client_data['client'] = client
        client_data['needs_code'] = False
        return jsonify({'message': 'Autenticado exitosamente', 'connected': True})
        
    except SessionPasswordNeededError:
        return jsonify({'error': 'Se requiere contraseña 2FA', 'needs_password': True}), 400
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"❌ Error al verificar código: {error_msg}")
        print(traceback.format_exc())
        return jsonify({'error': error_msg}), 500

@app.route('/api/status', methods=['GET'])
def status():
    """Verificar estado de conexión"""
    print(f"🔍 Verificando estado - Session phone: {session.get('phone', 'No hay')}")
    
    if 'phone' not in session:
        print("❌ No hay phone en sesión")
        return jsonify({'connected': False})
    
    phone = session['phone']
    print(f"📱 Phone en sesión: {phone}")
    print(f"📋 Clientes disponibles: {list(telegram_clients.keys())}")
    
    if phone in telegram_clients:
        client_data = telegram_clients[phone]
        print(f"✅ Cliente encontrado para {phone}")
        
        # Verificar si hay un cliente guardado y si está autorizado
        if 'client' in client_data and client_data['client'] is not None:
            client = client_data['client']
            try:
                # Verificar si está conectado y autorizado sin reconectar
                if client.is_connected():
                    is_authorized = run_async(client.is_user_authorized())
                    print(f"🔐 Cliente conectado, autorizado: {is_authorized}")
                    return jsonify({'connected': is_authorized})
                else:
                    # Intentar reconectar
                    print("🔄 Cliente desconectado, intentando reconectar...")
                    run_async(client.connect())
                    is_authorized = run_async(client.is_user_authorized())
                    print(f"🔐 Reconectado, autorizado: {is_authorized}")
                    return jsonify({'connected': is_authorized})
            except Exception as e:
                print(f"❌ Error verificando cliente: {e}")
                # Si hay error, verificar si la sesión existe en disco
                if os.path.exists(client_data['session_name'] + '.session'):
                    print("✅ Sesión existe en disco, considerando conectado")
                    return jsonify({'connected': True})
                return jsonify({'connected': False})
        else:
            # No hay cliente en memoria, pero verificar si existe sesión en disco
            if os.path.exists(client_data['session_name'] + '.session'):
                print("✅ Sesión existe en disco, considerando conectado")
                return jsonify({'connected': True})
            print("❌ No hay cliente ni sesión")
            return jsonify({'connected': False})
    
    # Verificar si existe una sesión guardada aunque no esté en memoria
    session_name = session.get('session_name', f"sessions/{secure_filename(phone)}")
    if os.path.exists(session_name + '.session'):
        print("✅ Sesión existe en disco, considerando conectado")
        return jsonify({'connected': True})
    
    print("❌ No hay conexión")
    return jsonify({'connected': False})

@app.route('/home')
def home():
    """Página principal estilo Telegram"""
    print(f"🏠 Página home - Session phone: {session.get('phone', 'No hay')}")
    
    # Verificar si hay phone en sesión - cada usuario debe tener su propia sesión
    if 'phone' not in session:
        print("❌ No hay phone en sesión, redirigiendo...")
        return redirect(url_for('index'))
    
    phone = session['phone']
    
    # Verificar que la sesión es válida para este usuario específico
    session_name = session.get('session_name', f"sessions/{secure_filename(phone)}")
    if session_name != f"sessions/{secure_filename(phone)}":
        print(f"❌ Sesión no coincide con el usuario, redirigiendo...")
        session.clear()
        return redirect(url_for('index'))
    
    # Si no está en telegram_clients, intentar cargarlo desde la sesión guardada
    if phone not in telegram_clients:
        if not os.path.exists(session_name + '.session'):
            print(f"❌ No hay cliente ni sesión para {phone}, redirigiendo...")
            session.clear()
            return redirect(url_for('index'))
    
    return render_template('home.html')

@app.route('/telegram-web')
def telegram_web():
    """Página con Telegram Web embebido (como contenedor)"""
    if 'phone' not in session:
        return redirect(url_for('index'))
    return render_template('telegram_web.html')

@app.route('/api/get_video_link', methods=['POST'])
def get_video_link():
    """Obtener o crear link de video desde chat_id y message_id"""
    if 'phone' not in session:
        return jsonify({'error': 'No estás conectado'}), 401
    
    data = request.get_json()
    chat_id = data.get('chat_id')
    message_id = data.get('message_id')
    
    if not chat_id or not message_id:
        return jsonify({'error': 'chat_id y message_id son requeridos'}), 400
    
    phone = session['phone']
    
    try:
        # Buscar si ya existe un video_id para este mensaje
        existing_video = find_video_by_message(chat_id, message_id, phone)
        
        if existing_video:
            video_id = existing_video['video_id']
            video_url = f'/watch/{video_id}'
            return jsonify({
                'video_id': video_id,
                'video_url': video_url,
                'watch_url': video_url
            })
        
        # Si no existe, crear uno nuevo obteniendo el mensaje
        client = get_or_create_client(phone)
        client_loop = client._loop
        
        async def get_message_info():
            target_chat = int(chat_id) if chat_id != 'me' and str(chat_id).isdigit() else 'me'
            message = await client.get_messages(target_chat, ids=message_id)
            return message
        
        try:
            message = run_async(get_message_info(), client_loop, timeout=30)
            
            if not message or not message.media:
                return jsonify({'error': 'Mensaje no encontrado o no tiene media'}), 404
            
            # Verificar que sea video
            is_video = False
            if hasattr(message.media, 'document'):
                doc = message.media.document
                if hasattr(doc, 'mime_type') and doc.mime_type and doc.mime_type.startswith('video/'):
                    is_video = True
                elif hasattr(doc, 'attributes'):
                    for attr in doc.attributes:
                        if isinstance(attr, DocumentAttributeVideo):
                            is_video = True
                            break
            
            if not is_video:
                return jsonify({'error': 'El mensaje no contiene un video'}), 400
            
            # Crear nuevo video_id
            video_id = secrets.token_urlsafe(16)
            chat_id_str = str(chat_id) if chat_id != 'me' else 'me'
            filename = 'video'
            if hasattr(message.media, 'document') and hasattr(message.media.document, 'attributes'):
                for attr in message.media.document.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        filename = attr.file_name
                        break
            
            file_size = None
            if hasattr(message.media, 'document'):
                file_size = message.media.document.size
            
            timestamp = int(time.time())
            
            # Guardar en base de datos
            if save_video_to_db(video_id, chat_id_str, message_id, filename, timestamp, file_size):
                video_url = f'/watch/{video_id}'
                return jsonify({
                    'video_id': video_id,
                    'video_url': video_url,
                    'watch_url': video_url
                })
            else:
                return jsonify({'error': 'Error al guardar el video'}), 500
                
        except Exception as e:
            print(f"❌ Error obteniendo mensaje: {e}")
            import traceback
            print(traceback.format_exc())
            return jsonify({'error': str(e)}), 500
            
    except Exception as e:
        print(f"❌ Error en get_video_link: {e}")
        import traceback
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/upload')
def upload_page():
    """Página de subida de videos (redirigir a home)"""
    return redirect(url_for('home'))

@app.route('/api/chats', methods=['GET'])
def get_chats():
    """Obtener lista de chats"""
    if 'phone' not in session:
        return jsonify({'error': 'No estás conectado'}), 401
    
    phone = session['phone']
    
    try:
        # Obtener cliente de forma segura
        client = get_or_create_client(phone)
        
        # CRÍTICO: Usar SIEMPRE el loop que el cliente tiene asignado internamente
        # NO intentar cambiarlo ni recrearlo - Telethon no lo permite
        client_loop = client._loop
        if not client_loop:
            print(f"❌ Cliente no tiene loop asignado, esto no debería pasar")
            return jsonify({'error': 'Error interno del cliente'}), 500
        
        # Si el loop está cerrado, el cliente no funcionará
        # En este caso, get_or_create_client debería haberlo manejado
        if client_loop.is_closed():
            print(f"❌ Loop del cliente está cerrado, esto no debería pasar después de get_or_create_client")
            return jsonify({'error': 'Error de conexión. Por favor, recarga la página.'}), 500
        
        async def fetch_chats_and_folders():
            chats = []
            chat_folders = []
            
            try:
                # Obtener carpetas de chat personalizadas (Dialog Filters)
                try:
                    filters_result = await client(GetDialogFiltersRequest())
                    print(f"🔍 Resultado de GetDialogFiltersRequest: {type(filters_result)}")
                    
                    # GetDialogFiltersRequest devuelve un objeto con una propiedad 'filters'
                    filters_list = []
                    if filters_result:
                        if hasattr(filters_result, 'filters'):
                            filters_list = filters_result.filters
                            print(f"✅ Encontradas {len(filters_list)} carpetas en filters_result.filters")
                        elif isinstance(filters_result, list):
                            filters_list = filters_result
                            print(f"✅ Encontradas {len(filters_list)} carpetas (lista directa)")
                        else:
                            print(f"⚠️ Formato inesperado: {filters_result}")
                    
                    for folder in filters_list:
                        # La estructura puede ser DialogFilter o DialogFilterSuggested
                        folder_filter = None
                        folder_id = None
                        
                        # Verificar si es DialogFilterSuggested (tiene .filter)
                        if hasattr(folder, 'filter') and folder.filter:
                            folder_filter = folder.filter
                            folder_id = folder.id if hasattr(folder, 'id') else None
                        # O si es directamente DialogFilter (tiene .title)
                        elif hasattr(folder, 'title'):
                            folder_filter = folder
                            folder_id = folder.id if hasattr(folder, 'id') else None
                        
                        if folder_filter:
                            # Obtener el emoji del icono si existe
                            icon_emoji = '📁'  # Emoji por defecto
                            if hasattr(folder_filter, 'icon_emoji_id') and folder_filter.icon_emoji_id:
                                # El icon_emoji_id es un ID numérico, necesitamos convertirlo a emoji
                                # Por ahora, usaremos un emoji por defecto
                                icon_emoji = '📁'
                            elif hasattr(folder_filter, 'icon_emoji') and folder_filter.icon_emoji:
                                icon_emoji = folder_filter.icon_emoji
                            
                            folder_info = {
                                'id': folder_id if folder_id is not None else len(chat_folders),
                                'title': folder_filter.title if hasattr(folder_filter, 'title') else f'Carpeta {len(chat_folders)}',
                                'icon_emoji': icon_emoji,
                                'include_peers': [],
                                'exclude_peers': []
                            }
                            
                            # Obtener chats incluidos en la carpeta
                            if hasattr(folder_filter, 'include_peers') and folder_filter.include_peers:
                                for peer in folder_filter.include_peers:
                                    if hasattr(peer, 'channel_id') and peer.channel_id:
                                        folder_info['include_peers'].append(peer.channel_id)
                                    elif hasattr(peer, 'chat_id') and peer.chat_id:
                                        folder_info['include_peers'].append(peer.chat_id)
                                    elif hasattr(peer, 'user_id') and peer.user_id:
                                        folder_info['include_peers'].append(peer.user_id)
                            
                            # Obtener chats excluidos de la carpeta
                            if hasattr(folder_filter, 'exclude_peers') and folder_filter.exclude_peers:
                                for peer in folder_filter.exclude_peers:
                                    if hasattr(peer, 'channel_id') and peer.channel_id:
                                        folder_info['exclude_peers'].append(peer.channel_id)
                                    elif hasattr(peer, 'chat_id') and peer.chat_id:
                                        folder_info['exclude_peers'].append(peer.chat_id)
                                    elif hasattr(peer, 'user_id') and peer.user_id:
                                        folder_info['exclude_peers'].append(peer.user_id)
                            
                            print(f"✅ Carpeta procesada: {folder_info['title']} con {len(folder_info['include_peers'])} chats incluidos")
                            chat_folders.append(folder_info)
                    
                    print(f"✅ Total carpetas de chat obtenidas: {len(chat_folders)}")
                except Exception as e:
                    import traceback
                    print(f"⚠️ No se pudieron obtener carpetas de chat (puede que no tengas carpetas creadas): {e}")
                    print(traceback.format_exc())
                
                # Obtener todos los chats
                async for dialog in client.iter_dialogs(limit=200):
                    # Determinar el tipo de chat usando la API de Telegram
                    entity = dialog.entity
                    chat_type = 'unknown'
                    
                    # Obtener ID de entidad (no diálogo) para mapear con carpetas
                    entity_id = None
                    if isinstance(entity, User):
                        chat_type = 'user'
                        entity_id = entity.id
                    elif isinstance(entity, Chat):
                        chat_type = 'group'
                        entity_id = entity.id
                    elif isinstance(entity, Channel):
                        if entity.broadcast:
                            chat_type = 'channel'
                        else:
                            chat_type = 'supergroup'
                        entity_id = entity.id
                    
                    chat_info = {
                        'id': dialog.id,  # ID de diálogo
                        'entity_id': entity_id,  # ID de entidad para mapear con carpetas
                        'name': dialog.name,
                        'type': chat_type,
                        'unread_count': dialog.unread_count,
                        'last_message': None,
                        'is_pinned': dialog.pinned,
                        'is_verified': False,
                        'is_premium': False
                    }
                    
                    # Información adicional según el tipo
                    if isinstance(entity, User):
                        chat_info['is_verified'] = getattr(entity, 'verified', False)
                        chat_info['is_premium'] = getattr(entity, 'premium', False)
                        chat_info['username'] = getattr(entity, 'username', None)
                    elif isinstance(entity, Channel):
                        chat_info['is_verified'] = getattr(entity, 'verified', False)
                        chat_info['username'] = getattr(entity, 'username', None)
                        chat_info['members_count'] = getattr(entity, 'participants_count', None)
                    
                    if dialog.message:
                        chat_info['last_message'] = {
                            'text': dialog.message.text[:50] if dialog.message.text else '',
                            'date': dialog.message.date.timestamp() if dialog.message.date else None
                        }
                    
                    chats.append(chat_info)
            except Exception as e:
                print(f"❌ Error en iter_dialogs: {e}")
                raise
            
            return chats, chat_folders
        
        # Ejecutar usando el loop del cliente
        try:
            chats, chat_folders = run_async(fetch_chats_and_folders(), client_loop)
            print(f"✅ Chats obtenidos: {len(chats)}")
            print(f"✅ Carpetas de chat: {len(chat_folders)}")
            
            # Crear un mapa de chats por ID para búsqueda rápida
            chats_by_id = {chat['id']: chat for chat in chats}
            
            # Organizar chats por carpetas personalizadas
            folders_with_chats = []
            for folder in chat_folders:
                folder_chats = []
                # Crear un set de IDs incluidos para búsqueda rápida
                included_peer_ids = set(folder['include_peers'])
                excluded_peer_ids = set(folder['exclude_peers'])
                
                # Buscar chats que estén incluidos y no excluidos
                for chat in chats:
                    entity_id = chat.get('entity_id')
                    if entity_id and entity_id in included_peer_ids and entity_id not in excluded_peer_ids:
                        folder_chats.append(chat)
                
                # Calcular estadísticas de la carpeta
                unread_count = sum(chat.get('unread_count', 0) for chat in folder_chats)
                
                folders_with_chats.append({
                    'id': folder['id'],
                    'title': folder['title'],
                    'icon_emoji': folder['icon_emoji'],
                    'chats': folder_chats,
                    'unread_count': unread_count,
                    'chat_count': len(folder_chats)
                })
            
            # Organizar chats por categorías automáticas (como Telegram)
            organized = {
                'all': chats,
                'users': [c for c in chats if c['type'] == 'user'],
                'groups': [c for c in chats if c['type'] in ['group', 'supergroup']],
                'channels': [c for c in chats if c['type'] == 'channel'],
                'pinned': [c for c in chats if c.get('is_pinned', False)]
            }
            
            return jsonify({
                'chats': chats,
                'organized': organized,
                'folders': folders_with_chats,  # Carpetas personalizadas
                'stats': {
                    'total': len(chats),
                    'users': len(organized['users']),
                    'groups': len(organized['groups']),
                    'channels': len(organized['channels']),
                    'pinned': len(organized['pinned']),
                    'folders': len(folders_with_chats)
                }
            })
        except Exception as e:
            print(f"❌ Error ejecutando fetch_chats: {e}")
            import traceback
            print(traceback.format_exc())
            raise
        
    except Exception as e:
        import traceback
        print(f"❌ Error obteniendo chats: {e}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

def get_or_create_client(phone):
    """Obtener o crear un cliente de Telegram para el teléfono dado.
    Asegura que el event loop esté correctamente configurado para evitar errores de "no current event loop".
    Obtener o crear cliente de forma segura, evitando bloqueos de base de datos.
    """
    if phone in telegram_clients:
        client_data = telegram_clients[phone]
        client = client_data.get('client')
        loop = client_data.get('loop')
        
        # Verificar que el cliente existe y el loop es válido
        if client:
            # PRIMERO verificar que el loop no esté cerrado
            if loop and not loop.is_closed():
                # CRÍTICO: Asegurarse de que el loop esté establecido como el loop actual del thread
                # Esto previene el error "no current event loop" cuando Flask reutiliza threads
                try:
                    asyncio.set_event_loop(loop)
                except RuntimeError:
                    # Si hay un error, el loop puede estar en mal estado, crear uno nuevo
                    del telegram_clients[phone]
                    loop = None
                else:
                    # El loop es válido, verificar conexión (is_connected es síncrono)
                    try:
                        # is_connected() es un método síncrono, no una corrutina
                        if client.is_connected():
                            # Cliente válido y conectado, retornarlo
                            return client
                        else:
                            print(f"⚠️ Cliente existe pero no está conectado, intentando reconectar...")
                            # Intentar reconectar
                            try:
                                run_async(client.connect(), loop, timeout=10)
                                # Verificar nuevamente (síncrono)
                                if client.is_connected():
                                    return client
                            except Exception as e:
                                print(f"⚠️ Error reconectando cliente: {e}")
                                # Si falla la reconexión, crear uno nuevo
                                del telegram_clients[phone]
                    except Exception as e:
                        print(f"⚠️ Error verificando conexión del cliente: {e}")
                        # Si hay error, el cliente puede estar en mal estado, crear uno nuevo
                        try:
                            # Intentar desconectar de forma segura
                            if hasattr(client, 'is_connected') and client.is_connected():
                                try:
                                    run_async(client.disconnect(), loop, timeout=5)
                                except:
                                    pass
                        except:
                            pass
                        del telegram_clients[phone]
            else:
                print(f"⚠️ Loop del cliente cerrado, necesitamos crear un nuevo cliente...")
                # El loop está cerrado, NO podemos usar el cliente
                # Intentar desconectar de forma segura (sin usar el loop cerrado)
                try:
                    # No intentar desconectar con un loop cerrado, simplemente limpiar
                    pass
                except:
                    pass
                # Limpiar el cliente viejo
                del telegram_clients[phone]
    
    # Si no hay cliente o no está conectado, crear uno nuevo
    # CRÍTICO: Usar lock para prevenir creación concurrente de clientes con la misma sesión SQLite
    # Esto previene el error "database is locked"
    session_name = session.get('session_name', f"sessions/{secure_filename(phone)}")
    api_id = int(session['api_id'])
    api_hash = session['api_hash']
    
    with _client_creation_lock:
        # Verificar nuevamente dentro del lock (double-check pattern)
        if phone in telegram_clients:
            client_data = telegram_clients[phone]
            client = client_data.get('client')
            if client and client.is_connected():
                return client
        
        # Cerrar cualquier cliente antiguo que pueda estar bloqueando la sesión
        # Buscar otros clientes que usen la misma sesión
        for other_phone, other_data in list(telegram_clients.items()):
            if other_phone != phone and other_data.get('session_name') == session_name:
                other_client = other_data.get('client')
                if other_client:
                    try:
                        print(f"🔒 Cerrando cliente antiguo para {other_phone} que usa la misma sesión...")
                        if other_client.is_connected():
                            other_loop = other_data.get('loop')
                            if other_loop and not other_loop.is_closed():
                                try:
                                    run_async(other_client.disconnect(), other_loop, timeout=5)
                                except:
                                    pass
                        del telegram_clients[other_phone]
                        print(f"✅ Cliente antiguo cerrado")
                    except Exception as e:
                        print(f"⚠️ Error cerrando cliente antiguo: {e}")
        
        # Esperar un momento para que SQLite libere el lock
        time.sleep(0.1)
        
        # Intentar crear el cliente con reintentos en caso de "database is locked"
        max_retries = 3
        retry_delay = 0.5
        
        for attempt in range(max_retries):
            try:
                # Usar un loop dedicado para este cliente
                loop = get_event_loop()
                client = TelegramClient(session_name, api_id, api_hash, loop=loop)
                
                # Conectar si no está conectado
                if not client.is_connected():
                    try:
                        run_async(client.connect(), loop, timeout=10)
                        if not client.is_connected():
                            print(f"⚠️ Cliente creado pero no conectado después de connect()")
                    except Exception as e:
                        error_msg = str(e).lower()
                        if 'database is locked' in error_msg or 'locked' in error_msg:
                            if attempt < max_retries - 1:
                                print(f"⚠️ Database locked, reintentando en {retry_delay}s (intento {attempt + 1}/{max_retries})...")
                                time.sleep(retry_delay)
                                retry_delay *= 2  # Backoff exponencial
                                continue
                        raise
                
                # Guardar en telegram_clients
                telegram_clients[phone] = {
                    'client': client,
                    'api_id': api_id,
                    'api_hash': api_hash,
                    'session_name': session_name,
                    'loop': loop
                }
                
                return client
                
            except Exception as e:
                error_msg = str(e).lower()
                if 'database is locked' in error_msg or 'locked' in error_msg:
                    if attempt < max_retries - 1:
                        print(f"⚠️ Database locked al crear cliente, reintentando en {retry_delay}s (intento {attempt + 1}/{max_retries})...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Backoff exponencial
                        continue
                    else:
                        print(f"❌ Error: database is locked después de {max_retries} intentos")
                        raise Exception("La base de datos de sesión está bloqueada. Por favor, espera unos segundos e intenta de nuevo.")
                else:
                    print(f"❌ Error conectando cliente en get_or_create_client: {e}")
                    import traceback
                    print(traceback.format_exc())
                    raise

@app.route('/api/chat/<chat_id>/messages', methods=['GET'])
def get_messages(chat_id):
    """Obtener mensajes de un chat"""
    if 'phone' not in session:
        return jsonify({'error': 'No estás conectado'}), 401
    
    phone = session['phone']
    limit = request.args.get('limit', 20, type=int)
    
    try:
        # Obtener cliente de forma segura
        client = get_or_create_client(phone)
        
        # CRÍTICO: Usar SIEMPRE el loop que el cliente tiene asignado internamente
        # NO intentar cambiarlo ni recrearlo - Telethon no lo permite
        client_loop = client._loop
        if not client_loop:
            print(f"❌ Cliente no tiene loop asignado, esto no debería pasar")
            return jsonify({'error': 'Error interno del cliente'}), 500
        
        # Si el loop está cerrado, el cliente no funcionará
        # En este caso, get_or_create_client debería haberlo manejado
        if client_loop.is_closed():
            print(f"❌ Loop del cliente está cerrado, esto no debería pasar después de get_or_create_client")
            return jsonify({'error': 'Error de conexión. Por favor, recarga la página.'}), 500
        
        async def fetch_messages():
            messages = []
            try:
                async for message in client.iter_messages(int(chat_id), limit=limit):
                    msg_info = {
                        'id': message.id,
                        'text': message.text or '',
                        'date': message.date.timestamp() if message.date else None,
                        'from_id': str(message.from_id) if message.from_id else None,
                        'media': None
                    }
                    
                    # Debug: mostrar información del mensaje
                    print(f"📨 Procesando mensaje {message.id}: text='{message.text[:50] if message.text else None}', has_media={message.media is not None}")
                    
                    # Si tiene media, guardar información
                    if message.media:
                        print(f"📎 Mensaje {message.id} tiene media: {type(message.media).__name__}")
                        if hasattr(message.media, 'photo'):
                            msg_info['media'] = {'type': 'photo'}
                        elif hasattr(message.media, 'document'):
                            doc = message.media.document
                            # Detectar si es video por mime_type o por atributos del documento
                            is_video = False
                            mime_type = None
                            
                            # Primero verificar mime_type
                            if hasattr(doc, 'mime_type') and doc.mime_type:
                                mime_type = doc.mime_type
                                is_video = mime_type.startswith('video/')
                            
                            # Verificar atributos del documento (más confiable)
                            if hasattr(doc, 'attributes') and doc.attributes:
                                for attr in doc.attributes:
                                    # Verificar si es DocumentAttributeVideo
                                    if isinstance(attr, DocumentAttributeVideo):
                                        is_video = True
                                        mime_type = mime_type or 'video/mp4'
                                        break
                                    # También verificar por nombre de clase como fallback
                                    elif hasattr(attr, '__class__'):
                                        class_name = attr.__class__.__name__
                                        if 'Video' in class_name:
                                            is_video = True
                                            mime_type = mime_type or 'video/mp4'
                                            break
                            
                            if is_video:
                                msg_info['media'] = {'type': 'video', 'mime_type': mime_type or 'video/mp4'}
                                print(f"🎬 Video detectado en mensaje {message.id}: mime_type={mime_type}")
                                
                                # Verificar si ya existe un video_id para este mensaje
                                chat_id_str = str(chat_id) if chat_id != 'me' else 'me'
                                existing_video_id = find_video_by_message(chat_id_str, message.id, phone)
                                
                                # Si no existe, crear uno nuevo
                                if not existing_video_id:
                                    video_id = secrets.token_urlsafe(16)
                                    # Obtener el nombre del archivo si está disponible
                                    filename = f"video_{message.id}.mp4"
                                    if hasattr(doc, 'attributes'):
                                        for attr in doc.attributes:
                                            if hasattr(attr, 'file_name') and attr.file_name:
                                                filename = attr.file_name
                                                break
                                    
                                    timestamp = message.date.timestamp() if message.date else time.time()
                                    file_size = doc.size if hasattr(doc, 'size') else None
                                    
                                    print(f"🆕 Creando nuevo video: chat_id={chat_id_str}, message_id={message.id}, video_id={video_id}")
                                    if save_video_to_db(video_id, chat_id_str, message.id, filename, timestamp, file_size):
                                        existing_video_id = video_id
                                        print(f"✅ Video nuevo registrado: Message={message.id}, Chat={chat_id_str}, VideoID={existing_video_id}, Filename={filename}")
                                    else:
                                        print(f"⚠️ Error guardando video en DB, pero continuando...")
                                        existing_video_id = video_id
                                else:
                                    print(f"✅ Video existente encontrado: {existing_video_id} (Message={message.id}, Chat={chat_id_str})")
                                
                                msg_info['video_url'] = f'/api/video/{existing_video_id}'
                                msg_info['video_id'] = existing_video_id  # Agregar video_id directamente
                                msg_info['watch_url'] = f'/watch/{existing_video_id}'
                                print(f"✅ Video URL asignado para mensaje {message.id}: {msg_info['video_url']}, video_id: {existing_video_id}")
                                
                                # Pre-cargar video en memoria en segundo plano (como Telegram - instantáneo)
                                def preload_video():
                                    try:
                                        if existing_video_id not in video_memory_cache:
                                            print(f"🔄 Pre-cargando video en memoria: {existing_video_id}")
                                            # Obtener cliente
                                            client_preload = get_or_create_client(phone)
                                            client_loop_preload = client_preload._loop
                                            
                                            # Obtener mensaje
                                            async def get_msg():
                                                target_chat_preload = int(chat_id) if chat_id != 'me' and str(chat_id).isdigit() else 'me'
                                                return await client_preload.get_messages(target_chat_preload, ids=message.id)
                                            
                                            msg_preload = run_async(get_msg(), client_loop_preload, timeout=30)
                                            
                                            if msg_preload and msg_preload.media:
                                                # Descargar a memoria
                                                async def download_to_mem():
                                                    buffer = BytesIO()
                                                    await client_preload.download_media(msg_preload, buffer)
                                                    buffer.seek(0)
                                                    return buffer.getvalue()
                                                
                                                video_data = run_async(download_to_mem(), client_loop_preload, timeout=300)
                                                if video_data:
                                                    video_memory_cache[existing_video_id] = video_data
                                                    print(f"✅ Video pre-cargado en memoria: {existing_video_id} ({len(video_data)} bytes)")
                                    except Exception as e:
                                        print(f"⚠️ Error pre-cargando video: {e}")
                                
                                # Pre-cargar en thread separado (no bloquea)
                                threading.Thread(target=preload_video, daemon=True).start()
                                
                                # Si el mensaje tiene texto (caption), mantenerlo pero no sobrescribir
                                if message.text and not msg_info.get('text'):
                                    msg_info['text'] = message.text
                    
                    # Agregar el mensaje a la lista (siempre, no solo si tiene media)
                    messages.append(msg_info)
            except Exception as e:
                print(f"❌ Error en iter_messages: {e}")
                import traceback
                print(traceback.format_exc())
                raise
            return messages
        
        # Ejecutar usando el loop del cliente
        try:
            messages = run_async(fetch_messages(), client_loop)
            print(f"✅ Mensajes obtenidos: {len(messages)}")
            return jsonify({'messages': messages})
        except Exception as e:
            print(f"❌ Error ejecutando fetch_messages: {e}")
            import traceback
            print(traceback.format_exc())
            raise
        
    except Exception as e:
        import traceback
        print(f"❌ Error obteniendo mensajes: {e}")
        print(traceback.format_exc())
        # Asegurarse de devolver JSON, no HTML
        return jsonify({'error': str(e)}), 500

@app.route('/api/send_message', methods=['POST'])
def send_message():
    """Enviar mensaje de texto"""
    if 'phone' not in session:
        return jsonify({'error': 'No estás conectado a Telegram'}), 401
    
    data = request.json
    chat_id = data.get('chat_id')
    text = data.get('text')
    
    if not chat_id or not text:
        return jsonify({'error': 'Faltan datos requeridos'}), 400
    
    phone = session['phone']
    
    try:
        # Obtener cliente de forma segura
        client = get_or_create_client(phone)
        
        # Usar el loop del cliente
        client_loop = client._loop
        
        async def send():
            message = await client.send_message(int(chat_id) if chat_id != 'me' else 'me', text)
            return {
                'id': message.id,
                'text': message.text or '',
                'date': message.date.timestamp() if message.date else None
            }
        
        # Ejecutar usando el loop del cliente
        message = run_async(send(), client_loop)
        return jsonify({'message': message})
        
    except Exception as e:
        import traceback
        print(f"❌ Error enviando mensaje: {e}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload_video():
    """Subir video a Telegram: guarda temporalmente en local, sube a la nube, y borra local"""
    # Asegurarse de que threading está disponible en el scope local
    import threading as threading_module
    
    print("=" * 80, flush=True)
    print("🚀 [UPLOAD] Endpoint /api/upload llamado", flush=True)
    print(f"📋 Método: {request.method}", flush=True)
    print(f"📋 Headers: {dict(request.headers)}", flush=True)
    print(f"📋 Form data keys: {list(request.form.keys())}", flush=True)
    print(f"📋 Files keys: {list(request.files.keys())}", flush=True)
    
    if 'phone' not in session:
        print("❌ [UPLOAD] Error: No hay sesión de Telegram", flush=True)
        return jsonify({'error': 'No estás conectado a Telegram'}), 401
    
    chat_id = request.form.get('chat_id', 'me')  # Por defecto a "me" (Saved Messages)
    description = request.form.get('description', '')  # Descripción opcional del video
    print(f"📋 Chat ID recibido: {chat_id}", flush=True)
    print(f"📋 Descripción: {description}", flush=True)
    
    if 'video' not in request.files:
        print("❌ [UPLOAD] Error: No se encontró 'video' en request.files", flush=True)
        return jsonify({'error': 'No se encontró el archivo de video'}), 400
    
    file = request.files['video']
    print(f"📁 [UPLOAD] Archivo obtenido de request.files", flush=True)
    
    if file.filename == '':
        print("❌ [UPLOAD] Error: filename vacío", flush=True)
        return jsonify({'error': 'No se seleccionó ningún archivo'}), 400
    
    print(f"📁 [UPLOAD] Archivo recibido: {file.filename}", flush=True)
    print(f"📁 [UPLOAD] Tipo de objeto file: {type(file)}", flush=True)
    print(f"📁 [UPLOAD] file.stream disponible: {hasattr(file, 'stream')}", flush=True)
    
    filename = secure_filename(file.filename)
    timestamp = int(time.time())
    upload_id = secrets.token_urlsafe(8)
    
    # Obtener valores de la sesión ANTES de crear el thread
    phone = session['phone']
    api_id = int(session['api_id'])
    api_hash = session['api_hash']
    session_name = session.get('session_name', f"sessions/{secure_filename(phone)}")
    
    # Inicializar progreso ANTES de guardar el archivo
    upload_progress[upload_id] = {'progress': 0, 'status': 'saving', 'current': 0, 'total': 0, 'message': 'Iniciando subida...'}
    print(f"✅ [UPLOAD] Upload ID creado: {upload_id}", flush=True)
    print(f"📋 [UPLOAD] Upload IDs disponibles después de crear: {list(upload_progress.keys())}", flush=True)
    
    # Guardar archivo temporalmente en local
    local_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{timestamp}_{filename}")
    
    # Actualizar estado para indicar que se está guardando el archivo
    upload_progress[upload_id]['status'] = 'saving'
    upload_progress[upload_id]['message'] = 'Guardando archivo en servidor...'
    
    # Obtener tamaño del archivo si está disponible
    file_size_from_request = None
    if hasattr(file, 'content_length') and file.content_length:
        file_size_from_request = file.content_length
    elif hasattr(file, 'content_length') and file.content_length == 0:
        # Intentar obtener el tamaño desde el stream
        try:
            file.seek(0, 2)  # Ir al final
            file_size_from_request = file.tell()
            file.seek(0)  # Volver al inicio
        except:
            pass
    
    print(f"💾 [UPLOAD] Iniciando guardado de archivo: {local_path}", flush=True)
    print(f"📦 [UPLOAD] Tamaño del archivo recibido: {file_size_from_request if file_size_from_request else 'desconocido'} bytes", flush=True)
    
    # Estimar el tamaño del archivo para el progreso inicial
    if file_size_from_request:
        upload_progress[upload_id]['total'] = file_size_from_request
    else:
        # Si no conocemos el tamaño, usar un estimado grande
        upload_progress[upload_id]['total'] = 4 * 1024 * 1024 * 1024  # 4GB estimado
    
    # CRÍTICO: Definir upload_in_background ANTES de usarla
    def upload_in_background(phone_param, api_id_param, api_hash_param, session_name_param, chat_id_param, local_path_param, filename_param, upload_id_param, timestamp_param, file_size_param, description_param=''):
        try:
            # STREAMING: Para archivos grandes (>50MB), empezar a subir cuando tengamos 50MB
            # Para archivos pequeños, esperar a que esté completamente guardado
            print(f"⏳ [UPLOAD-BG] Esperando a que el archivo esté listo: {local_path_param}", flush=True)
            
            min_size_to_start = 50 * 1024 * 1024  # 50MB mínimo para empezar streaming
            max_wait_time = 300  # 5 minutos máximo esperando
            wait_interval = 0.5  # Verificar cada medio segundo para respuesta más rápida
            waited = 0
            last_size = 0
            stable_count = 0  # Contador para detectar cuando el archivo deja de crecer
            
            # Esperar a que el archivo exista y esté listo
            while waited < max_wait_time:
                if os.path.exists(local_path_param):
                    current_size = os.path.getsize(local_path_param)
                    
                    # Si el archivo es pequeño (<50MB), esperar a que esté completamente guardado
                    # Detectamos esto cuando el tamaño deja de cambiar
                    if file_size_param > 0 and file_size_param < min_size_to_start:
                        # Archivo pequeño: esperar a que esté completamente guardado
                        if current_size >= file_size_param:
                            print(f"✅ [UPLOAD-BG] Archivo pequeño completamente guardado ({current_size / (1024*1024):.1f}MB), iniciando subida...", flush=True)
                            break
                        elif current_size == last_size:
                            stable_count += 1
                            # Si el tamaño no cambia por 2 segundos, asumir que está completo
                            if stable_count >= 4:  # 4 * 0.5s = 2 segundos
                                print(f"✅ [UPLOAD-BG] Archivo pequeño parece estar completo ({current_size / (1024*1024):.1f}MB), iniciando subida...", flush=True)
                                break
                        else:
                            stable_count = 0
                    else:
                        # Archivo grande: empezar cuando tengamos 50MB
                        if current_size >= min_size_to_start:
                            print(f"✅ [UPLOAD-BG] Archivo grande tiene tamaño suficiente ({current_size / (1024*1024):.1f}MB), iniciando subida...", flush=True)
                            break
                        elif current_size > 0:
                            # Si tiene algo pero no suficiente, esperar un poco más
                            if upload_id_param in upload_progress:
                                upload_progress[upload_id_param]['message'] = f'Preparando archivo... ({current_size / (1024*1024):.1f}MB)'
                    
                    last_size = current_size
                else:
                    stable_count = 0
                
                # Verificar errores
                if upload_id_param in upload_progress:
                    status = upload_progress[upload_id_param].get('status', 'unknown')
                    if status == 'error':
                        error_msg = upload_progress[upload_id_param].get('error', 'Error desconocido')
                        raise Exception(f"Error durante el guardado: {error_msg}")
                
                time.sleep(wait_interval)
                waited += wait_interval
            
            if waited >= max_wait_time:
                # Si no alcanzó el tamaño mínimo, intentar de todas formas si existe
                if os.path.exists(local_path_param):
                    current_size = os.path.getsize(local_path_param)
                    if current_size > 0:
                        print(f"⚠️ [UPLOAD-BG] Timeout esperando, pero archivo existe ({current_size / (1024*1024):.1f}MB), iniciando subida...", flush=True)
                    else:
                        raise Exception("El archivo existe pero está vacío")
                else:
                    raise Exception("Timeout esperando a que el archivo exista (5 minutos)")
            
            # Verificar que el archivo existe
            if not os.path.exists(local_path_param):
                raise Exception(f"El archivo no existe: {local_path_param}")
            
            # Obtener el tamaño actual del archivo (puede seguir creciendo)
            actual_file_size = os.path.getsize(local_path_param)
            if file_size_param == 0:
                file_size_param = actual_file_size
            if upload_id_param in upload_progress:
                upload_progress[upload_id_param]['total'] = file_size_param
                print(f"✅ [UPLOAD-BG] Iniciando subida: archivo tiene {actual_file_size} bytes ({actual_file_size / (1024*1024*1024):.2f} GB)", flush=True)
            
            # Asegurarse de que el upload_id existe ANTES de comenzar la subida
            if upload_id_param not in upload_progress:
                print(f"⚠️ [UPLOAD-BG] Upload ID {upload_id_param} no encontrado al iniciar background, inicializando...", flush=True)
                upload_progress[upload_id_param] = {
                    'progress': 30,  # 30% porque ya se guardó el mínimo
                    'status': 'uploading', 
                    'current': 0, 
                    'total': file_size_param
                }
            else:
                # Asegurarse de que el total esté configurado
                upload_progress[upload_id_param]['total'] = file_size_param
                upload_progress[upload_id_param]['status'] = 'uploading'
                upload_progress[upload_id_param]['progress'] = 30  # 30% = guardado inicial completo
                upload_progress[upload_id_param]['message'] = 'Subiendo a Telegram...'
            
            print(f"🚀 [UPLOAD-BG] Iniciando subida en background - Upload ID: {upload_id_param}", flush=True)
            print(f"📋 [UPLOAD-BG] Upload IDs disponibles al iniciar background: {list(upload_progress.keys())}", flush=True)
            
            # IMPORTANTE: NO crear un cliente nuevo con la misma sesión SQLite porque causa "database is locked"
            # En su lugar, usar el cliente principal pero ejecutar la subida de forma asíncrona
            # Obtener el cliente principal del diccionario
            if phone_param not in telegram_clients:
                raise Exception("Cliente no encontrado en telegram_clients")
            
            client_data = telegram_clients[phone_param]
            client = client_data.get('client')
            client_loop = client_data.get('loop')
            
            if not client or not client_loop:
                raise Exception("Cliente o loop no disponible")
            
            # Verificar que el cliente esté conectado
            if not client.is_connected():
                print(f"⚠️ Cliente no conectado, intentando conectar...")
                run_async(client.connect(), client_loop, timeout=10)
                if not client.is_connected():
                    raise Exception("No se pudo conectar el cliente")
            
            print(f"✅ Usando cliente principal para subida en background")
            
            # Callback para el progreso
            # El progreso de Telegram (0-100%) se mapea a 30-100% del progreso total
            # porque el guardado inicial (0-30%) ya se completó
            def progress_callback(current, total):
                if total > 0:
                    # Progreso de Telegram (0-100%)
                    telegram_progress = (current / total) * 100
                    # Mapear a progreso total: 30% (guardado inicial) + 70% * progreso_telegram
                    total_progress = 30 + int(telegram_progress * 0.7)
                    
                    # Asegurarse de que el upload_id existe en el diccionario
                    if upload_id_param not in upload_progress:
                        print(f"⚠️ Upload ID {upload_id_param} no encontrado en callback, inicializando...", flush=True)
                        upload_progress[upload_id_param] = {
                            'progress': 30,  # Guardado inicial
                            'status': 'uploading', 
                            'current': current, 
                            'total': total
                        }
                    
                    # Actualizar progreso de forma thread-safe
                    upload_progress[upload_id_param]['progress'] = total_progress
                    upload_progress[upload_id_param]['current'] = current
                    upload_progress[upload_id_param]['total'] = total
                    upload_progress[upload_id_param]['status'] = 'uploading'
                    upload_progress[upload_id_param]['message'] = f'Subiendo a Telegram... {total_progress}%'
                    
                    # Loggear cada 5% para no saturar
                    if total_progress % 5 == 0 or total_progress == 100:
                        print(f"📤 [UPLOAD-BG] Progreso total: {total_progress}% (Telegram: {telegram_progress:.1f}%, {current}/{total} bytes) - Upload ID: {upload_id_param}", flush=True)
            
            # Subir video al chat especificado desde el archivo local
            async def upload():
                # Usar la descripción si está disponible, sino usar el nombre del archivo
                caption = description_param if description_param else filename_param
                
                # Enviar el archivo desde el path local
                message = await client.send_file(
                    int(chat_id_param) if chat_id_param != 'me' else 'me', 
                    local_path_param, 
                    caption=caption,
                    progress_callback=progress_callback
                )
                return message
            
            # Ejecutar usando el loop del cliente con timeout largo para videos grandes
            # Calcular timeout basado en el tamaño del archivo (6 segundos por MB, mínimo 10 minutos)
            file_size_mb = file_size_param / (1024 * 1024)
            timeout_seconds = max(600, int(file_size_mb * 6))  # Mínimo 10 minutos, 6 segundos por MB
            print(f"⏱️ [UPLOAD-BG] Timeout configurado: {timeout_seconds} segundos para archivo de {file_size_mb:.2f} MB", flush=True)
            print(f"🚀 [UPLOAD-BG] Iniciando subida a Telegram ahora...", flush=True)
            
            try:
                message = run_async(upload(), client_loop, timeout=timeout_seconds)
                print(f"✅ [UPLOAD-BG] Subida completada exitosamente", flush=True)
            except Exception as upload_error:
                error_msg = str(upload_error)
                print(f"❌ [UPLOAD-BG] Error durante la subida: {error_msg}", flush=True)
                import traceback
                print(f"❌ [UPLOAD-BG] Traceback:\n{traceback.format_exc()}", flush=True)
                raise
            
            # Marcar como completado
            upload_progress[upload_id_param]['status'] = 'completed'
            upload_progress[upload_id_param]['progress'] = 100
            
            # ✅ CONFIRMADO: Video subido a la nube de Telegram
            # Ahora borrar el archivo local inmediatamente
            try:
                if os.path.exists(local_path_param):
                    os.remove(local_path_param)
                    print(f"🗑️ Archivo local eliminado: {local_path_param}")
                else:
                    print(f"⚠️ Archivo no existe (ya fue eliminado): {local_path_param}")
            except Exception as e:
                print(f"⚠️ Error eliminando archivo local: {e}")
                import traceback
                print(traceback.format_exc())
                # Intentar de nuevo después de un segundo
                try:
                    time.sleep(1)
                    if os.path.exists(local_path_param):
                        os.remove(local_path_param)
                        print(f"🗑️ Archivo local eliminado en segundo intento: {local_path_param}")
                except Exception as e2:
                    print(f"❌ Error crítico eliminando archivo: {e2}")
            
            # Generar URL alternativa para ver el video
            video_id = secrets.token_urlsafe(16)
            # Asegurarse de que chat_id sea string para consistencia
            chat_id_str = str(chat_id_param) if chat_id_param != 'me' else 'me'
            
            if save_video_to_db(video_id, chat_id_str, message.id, filename_param, timestamp_param, file_size_param):
                print(f"✅ Video subido a Telegram: ID={video_id}, Chat={chat_id_str}, Message={message.id}")
            else:
                print(f"⚠️ Error guardando video en DB, pero continuando...")
            
            # Guardar video_id en el progreso para que el frontend lo pueda obtener
            upload_progress[upload_id_param]['video_id'] = video_id
            upload_progress[upload_id_param]['message_id'] = message.id
            upload_progress[upload_id_param]['chat_id'] = chat_id_str
            
        except Exception as e:
            # Marcar como error con información detallada
            import traceback
            error_traceback = traceback.format_exc()
            error_msg = f"{type(e).__name__}: {str(e)}"
            
            print(f"❌ ERROR en subida en background - Upload ID: {upload_id_param}")
            print(f"❌ Error: {error_msg}")
            print(f"❌ Traceback completo:\n{error_traceback}")
            
            if upload_id_param in upload_progress:
                upload_progress[upload_id_param]['status'] = 'error'
                upload_progress[upload_id_param]['error'] = error_msg
                upload_progress[upload_id_param]['error_details'] = error_traceback
            
            # Limpiar archivo local en caso de error
            try:
                if os.path.exists(local_path_param):
                    os.remove(local_path_param)
                    print(f"🗑️ Archivo local eliminado después de error: {local_path_param}")
                else:
                    print(f"⚠️ Archivo no existe para eliminar: {local_path_param}")
            except Exception as e:
                print(f"⚠️ Error eliminando archivo después de error: {e}")
                # Intentar de nuevo
                try:
                    time.sleep(1)
                    if os.path.exists(local_path_param):
                        os.remove(local_path_param)
                        print(f"🗑️ Archivo local eliminado en segundo intento: {local_path_param}")
                except:
                    pass
    
    # SOLUCIÓN STREAMING: Guardar y subir simultáneamente
    # Leemos el archivo en chunks y lo guardamos, pero empezamos a subir tan pronto como tengamos suficiente data
    def save_and_upload_streaming(file_obj, save_path, upload_id_param, estimated_size, phone_param, api_id_param, api_hash_param, session_name_param, chat_id_param, filename_param, description_param):
        try:
            import shutil
            chunk_size = 10 * 1024 * 1024  # 10MB chunks para mejor rendimiento
            total_saved = 0
            min_size_to_start_upload = 50 * 1024 * 1024  # Empezar a subir cuando tengamos 50MB guardados
            
            print(f"🚀 [STREAMING] Iniciando guardado y subida simultánea para upload_id: {upload_id_param}", flush=True)
            print(f"💾 [STREAMING] Ruta destino: {save_path}", flush=True)
            
            # Si file_obj es None, el archivo ya está guardado en save_path
            if file_obj is None:
                print(f"✅ [STREAMING] Archivo ya está guardado, verificando tamaño...", flush=True)
                if os.path.exists(save_path):
                    actual_file_size = os.path.getsize(save_path)
                    upload_progress[upload_id_param]['total'] = actual_file_size
                    upload_progress[upload_id_param]['status'] = 'uploading'
                    upload_progress[upload_id_param]['message'] = 'Subiendo a Telegram...'
                    upload_progress[upload_id_param]['progress'] = 30
                    
                    # Iniciar subida directamente
                    import threading as threading_module
                    upload_thread = threading_module.Thread(
                        target=lambda: upload_in_background(
                            phone_param, api_id_param, api_hash_param, session_name_param,
                            chat_id_param, save_path, filename_param, upload_id_param,
                            int(time.time()), actual_file_size, description_param
                        ),
                        daemon=True
                    )
                    upload_thread.start()
                    upload_thread.join(timeout=3600)  # Esperar máximo 1 hora
                    return
                else:
                    raise Exception("El archivo no existe en la ruta especificada")
            
            # Resetear el stream al inicio si es posible
            try:
                file_obj.seek(0)
            except:
                pass  # Algunos streams no soportan seek
            
            # Guardar en archivo temporal mientras leemos
            with open(save_path, 'wb') as f:
                upload_started = False
                upload_thread = None
                
                while True:
                    try:
                        chunk = file_obj.read(chunk_size)
                        if not chunk:
                            break
                    except Exception as read_error:
                        print(f"❌ [STREAMING] Error leyendo del stream: {read_error}", flush=True)
                        # Si el stream está cerrado, intentar continuar con lo que ya tenemos
                        if total_saved > 0:
                            print(f"⚠️ [STREAMING] Stream cerrado, pero ya tenemos {total_saved} bytes guardados", flush=True)
                            break
                        else:
                            raise
                    
                    f.write(chunk)
                    total_saved += len(chunk)
                    f.flush()  # Asegurar que se escriba al disco inmediatamente
                    
                    # Actualizar progreso
                    if estimated_size and estimated_size > 0:
                        save_progress = int((total_saved / estimated_size) * 30)  # Máximo 30% para guardado inicial
                        upload_progress[upload_id_param]['progress'] = save_progress
                        upload_progress[upload_id_param]['current'] = total_saved
                        upload_progress[upload_id_param]['total'] = estimated_size
                        upload_progress[upload_id_param]['status'] = 'saving'
                        upload_progress[upload_id_param]['message'] = f'Preparando archivo... {save_progress}%'
                        
                        # Loggear cada 5%
                        if save_progress % 5 == 0:
                            mb_saved = total_saved / (1024 * 1024)
                            mb_total = estimated_size / (1024 * 1024)
                            print(f"💾 [STREAMING] Guardando: {save_progress}% ({mb_saved:.1f}MB/{mb_total:.1f}MB)", flush=True)
                    else:
                        mb_saved = total_saved / (1024 * 1024)
                        upload_progress[upload_id_param]['current'] = total_saved
                        upload_progress[upload_id_param]['message'] = f'Preparando archivo... ({mb_saved:.1f}MB)'
                        if int(mb_saved) % 50 == 0:
                            print(f"💾 [STREAMING] Guardando: {mb_saved:.1f}MB guardados...", flush=True)
                    
                    # Si tenemos suficiente data y aún no empezamos la subida, iniciarla
                    if not upload_started and total_saved >= min_size_to_start_upload:
                        print(f"🚀 [STREAMING] Iniciando subida a Telegram mientras se guarda el resto...", flush=True)
                        upload_progress[upload_id_param]['status'] = 'uploading'
                        upload_progress[upload_id_param]['message'] = 'Subiendo a Telegram mientras se guarda...'
                        
                        # Iniciar subida en thread separado
                        import threading as threading_module
                        upload_thread = threading_module.Thread(
                            target=lambda: upload_in_background(
                                phone_param, api_id_param, api_hash_param, session_name_param,
                                chat_id_param, save_path, filename_param, upload_id_param,
                                int(time.time()), estimated_size or total_saved, description_param
                            ),
                            daemon=True
                        )
                        upload_thread.start()
                        upload_started = True
            
            # Verificar tamaño real
            actual_file_size = os.path.getsize(save_path)
            upload_progress[upload_id_param]['total'] = actual_file_size
            
            if not upload_started:
                # Si el archivo es muy pequeño, iniciar subida ahora
                print(f"🚀 [STREAMING] Archivo pequeño, iniciando subida ahora...", flush=True)
                upload_progress[upload_id_param]['status'] = 'uploading'
                upload_progress[upload_id_param]['message'] = 'Subiendo a Telegram...'
                upload_progress[upload_id_param]['progress'] = 30
                
                import threading as threading_module
                upload_thread = threading_module.Thread(
                    target=lambda: upload_in_background(
                        phone_param, api_id_param, api_hash_param, session_name_param,
                        chat_id_param, save_path, filename_param, upload_id_param,
                        int(time.time()), actual_file_size, description_param
                    ),
                    daemon=True
                )
                upload_thread.start()
            else:
                # Esperar a que termine la subida
                print(f"⏳ [STREAMING] Esperando a que termine la subida...", flush=True)
                if upload_thread:
                    upload_thread.join(timeout=3600)  # Máximo 1 hora
            
            print(f"✅ [STREAMING] Proceso completado: {save_path} ({actual_file_size} bytes, {actual_file_size / (1024*1024*1024):.2f} GB)", flush=True)
            
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            error_msg = f"Error en streaming: {str(e)}"
            print(f"❌ [STREAMING] {error_msg}", flush=True)
            print(f"❌ [STREAMING] Traceback:\n{error_traceback}", flush=True)
            if upload_id_param in upload_progress:
                upload_progress[upload_id_param]['status'] = 'error'
                upload_progress[upload_id_param]['error'] = error_msg
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                    print(f"🗑️ [STREAMING ERROR] Archivo eliminado: {save_path}", flush=True)
                except Exception as cleanup_e:
                    print(f"⚠️ [STREAMING ERROR] Error eliminando archivo: {cleanup_e}", flush=True)
    
    # ULTRA POTENTE: Para archivos grandes, usar streaming directo sin cargar en memoria
    # Para archivos pequeños, podemos usar buffer en memoria
    # Umbral: 100MB - archivos más grandes usan streaming directo
    STREAMING_THRESHOLD = 100 * 1024 * 1024  # 100MB
    
    # Obtener tamaño del archivo si está disponible
    file_size_from_request = None
    if hasattr(file, 'content_length') and file.content_length:
        file_size_from_request = file.content_length
    elif hasattr(file, 'content_length') and file.content_length == 0:
        # Intentar obtener el tamaño desde el stream
        try:
            file.seek(0, 2)  # Ir al final
            file_size_from_request = file.tell()
            file.seek(0)  # Volver al inicio
        except:
            pass
    
    # Decidir estrategia basado en tamaño
    use_streaming = file_size_from_request is None or file_size_from_request >= STREAMING_THRESHOLD
    
    if use_streaming:
        # STREAMING ULTRA POTENTE: Para archivos grandes, leer en chunks ANTES de devolver respuesta
        # Esto asegura que el stream esté disponible y el progreso se actualice desde el inicio
        print(f"🚀 [UPLOAD] Modo STREAMING ULTRA POTENTE activado para archivo grande", flush=True)
        print(f"📦 [UPLOAD] Tamaño estimado: {file_size_from_request / (1024*1024*1024):.2f} GB" if file_size_from_request else "📦 [UPLOAD] Tamaño desconocido (streaming)", flush=True)
        
        # CRÍTICO: Inicializar progreso ANTES de empezar a leer
        upload_progress[upload_id]['status'] = 'saving'
        upload_progress[upload_id]['message'] = 'Guardando archivo...'
        upload_progress[upload_id]['progress'] = 0
        
        # ULTRA POTENTE: Leer el stream en chunks ANTES de devolver la respuesta
        # Esto permite actualizar el progreso en tiempo real desde el inicio
        print(f"💾 [UPLOAD] Leyendo archivo en chunks con progreso en tiempo real...", flush=True)
        
        file.seek(0)  # Asegurarse de que estamos al inicio
        file_stream = file.stream
        chunk_size = 10 * 1024 * 1024  # 10MB chunks
        total_read = 0
        last_logged_progress = -1
        
        # Leer y guardar en chunks, actualizando progreso en tiempo real
        with open(local_path, 'wb') as f:
            while True:
                try:
                    chunk = file_stream.read(chunk_size)
                    if not chunk:
                        break
                    
                    f.write(chunk)
                    f.flush()  # Asegurar escritura inmediata
                    total_read += len(chunk)
                    
                    # Actualizar progreso en tiempo real - CRÍTICO para archivos grandes
                    if file_size_from_request and file_size_from_request > 0:
                        save_progress = min(30, int((total_read / file_size_from_request) * 30))
                        upload_progress[upload_id]['progress'] = save_progress
                        upload_progress[upload_id]['current'] = total_read
                        upload_progress[upload_id]['total'] = file_size_from_request
                        upload_progress[upload_id]['status'] = 'saving'
                        upload_progress[upload_id]['message'] = f'Guardando archivo... {save_progress}%'
                        
                        # Loggear cada 1% para archivos grandes
                        if save_progress != last_logged_progress and (save_progress % 1 == 0 or save_progress == 30):
                            mb_saved = total_read / (1024 * 1024)
                            mb_total = file_size_from_request / (1024 * 1024)
                            print(f"💾 [UPLOAD] Guardando: {save_progress}% ({mb_saved:.1f}MB/{mb_total:.1f}MB)", flush=True)
                            last_logged_progress = save_progress
                    else:
                        mb_saved = total_read / (1024 * 1024)
                        upload_progress[upload_id]['current'] = total_read
                        upload_progress[upload_id]['message'] = f'Guardando archivo... ({mb_saved:.1f}MB)'
                        if int(mb_saved) % 50 == 0:
                            print(f"💾 [UPLOAD] Guardando: {mb_saved:.1f}MB guardados...", flush=True)
                
                except Exception as read_error:
                    print(f"❌ [UPLOAD] Error leyendo del stream: {read_error}", flush=True)
                    if total_read > 0:
                        print(f"⚠️ [UPLOAD] Stream cerrado, pero ya tenemos {total_read} bytes guardados", flush=True)
                        break
                    else:
                        raise
        
        actual_file_size = os.path.getsize(local_path)
        print(f"✅ [UPLOAD] Archivo guardado completamente: {local_path} ({actual_file_size} bytes, {actual_file_size / (1024*1024*1024):.2f} GB)", flush=True)
        
        # Actualizar progreso final del guardado
        upload_progress[upload_id]['total'] = actual_file_size
        upload_progress[upload_id]['status'] = 'saved'
        upload_progress[upload_id]['message'] = 'Archivo guardado, iniciando subida a Telegram...'
        upload_progress[upload_id]['progress'] = 30
        
        # CRÍTICO: Devolver respuesta DESPUÉS de guardar el archivo
        # Esto asegura que el stream esté completamente leído antes de que Flask lo cierre
        print(f"📤 [UPLOAD] Devolviendo respuesta con upload_id: {upload_id}", flush=True)
        print(f"📋 [UPLOAD] Upload IDs disponibles: {list(upload_progress.keys())}", flush=True)
        
        # Iniciar subida a Telegram en background
        import threading as threading_module
        upload_thread = threading_module.Thread(
            target=lambda: upload_in_background(
                phone, api_id, api_hash, session_name,
                chat_id, local_path, filename, upload_id,
                timestamp, actual_file_size, description
            ),
            daemon=True
        )
        upload_thread.start()
        print(f"🧵 [UPLOAD] Thread de subida a Telegram iniciado", flush=True)
    
    else:
        # Para archivos pequeños, usar buffer en memoria (más rápido)
        print(f"💾 [UPLOAD] Modo buffer en memoria para archivo pequeño", flush=True)
        from io import BytesIO
        import shutil
        
        print(f"💾 [UPLOAD] Copiando archivo a buffer en memoria...", flush=True)
        file.seek(0)  # Asegurarse de que estamos al inicio
        file_buffer = BytesIO()
        shutil.copyfileobj(file.stream, file_buffer)
        file_buffer.seek(0)  # Resetear el buffer para que el thread pueda leerlo desde el inicio
        file_size_bytes = file_buffer.getbuffer().nbytes
        print(f"✅ [UPLOAD] Archivo copiado a buffer: {file_size_bytes} bytes ({file_size_bytes / (1024*1024):.2f} MB)", flush=True)
        
        # CRÍTICO: Devolver respuesta INMEDIATAMENTE después de copiar el archivo
        print(f"📤 [UPLOAD] Devolviendo respuesta INMEDIATA con upload_id: {upload_id}", flush=True)
        print(f"📋 [UPLOAD] Upload IDs disponibles: {list(upload_progress.keys())}", flush=True)
        
        # IMPORTANTE: Iniciar procesamiento en background DESPUÉS de devolver la respuesta
        import threading as threading_module
        
        def process_upload_background():
            try:
                print(f"🚀 [BG] Iniciando procesamiento en background para upload_id: {upload_id}", flush=True)
                
                # Actualizar estado
                upload_progress[upload_id]['status'] = 'saving'
                upload_progress[upload_id]['message'] = 'Guardando archivo...'
                upload_progress[upload_id]['progress'] = 0
                
                # Guardar el archivo desde el buffer en memoria
                print(f"💾 [BG] Guardando archivo desde buffer en memoria...", flush=True)
                file_buffer.seek(0)  # Asegurarse de que estamos al inicio del buffer
                with open(local_path, 'wb') as f:
                    shutil.copyfileobj(file_buffer, f)
                
                actual_file_size = os.path.getsize(local_path)
                print(f"✅ [BG] Archivo guardado: {local_path} ({actual_file_size} bytes, {actual_file_size / (1024*1024*1024):.2f} GB)", flush=True)
                
                # Actualizar progreso
                upload_progress[upload_id]['total'] = actual_file_size
                upload_progress[upload_id]['status'] = 'saved'
                upload_progress[upload_id]['message'] = 'Archivo guardado, iniciando subida a Telegram...'
                upload_progress[upload_id]['progress'] = 30
                
                # Iniciar subida a Telegram inmediatamente
                print(f"📤 [BG] Iniciando subida a Telegram...", flush=True)
                upload_in_background(
                    phone, api_id, api_hash, session_name,
                    chat_id, local_path, filename, upload_id,
                    timestamp, actual_file_size, description
                )
            except Exception as e:
                import traceback
                error_traceback = traceback.format_exc()
                error_msg = f"Error procesando upload: {str(e)}"
                print(f"❌ [BG] {error_msg}", flush=True)
                print(f"❌ [BG] Traceback:\n{error_traceback}", flush=True)
                if upload_id in upload_progress:
                    upload_progress[upload_id]['status'] = 'error'
                    upload_progress[upload_id]['error'] = error_msg
        
        # Iniciar thread de procesamiento en background
        process_thread = threading_module.Thread(target=process_upload_background, daemon=True)
        process_thread.start()
        print(f"🧵 [UPLOAD] Thread de procesamiento iniciado", flush=True)
    
    # CRÍTICO: Devolver respuesta INMEDIATAMENTE después de iniciar el thread
    # Esto permite que el frontend reciba la respuesta mientras Flask aún está recibiendo el request
    print(f"📤 [UPLOAD] Devolviendo respuesta INMEDIATAMENTE con upload_id: {upload_id}", flush=True)
    print(f"📋 [UPLOAD] Upload IDs disponibles: {list(upload_progress.keys())}", flush=True)
    print("=" * 80, flush=True)
    
    return jsonify({
        'message': 'Subida iniciada',
        'upload_id': upload_id,
        'status': 'saving'
    })
    

@app.route('/api/upload/progress/<upload_id>', methods=['GET'])
def get_upload_progress(upload_id):
    """Obtener progreso de subida"""
    # No loggear cada consulta para evitar spam en los logs
    # Solo loggear si hay un problema
    
    if upload_id in upload_progress:
        progress_data = upload_progress[upload_id].copy()
        return jsonify(progress_data)
    
    # Si no se encuentra, puede ser que aún no se haya inicializado
    # Devolver estado inicial en lugar de 404
    # Loggear solo ocasionalmente para no saturar los logs
    import random
    if random.random() < 0.1:  # Solo 10% de las veces
        print(f"⚠️ Upload ID {upload_id} no encontrado")
        print(f"📋 Upload IDs disponibles: {list(upload_progress.keys())}")
    return jsonify({'progress': 0, 'status': 'uploading', 'current': 0, 'total': 0})

@app.route('/watch/<video_id>')
def watch_video(video_id):
    """Página para ver el video"""
    video_info = get_video_from_db(video_id)
    if not video_info:
        return "Video no encontrado", 404
    
    return render_template('watch.html', video_id=video_id)

@app.route('/api/video/<video_id>/thumbnail')
def get_video_thumbnail(video_id):
    """Obtener la miniatura del video (como Telegram Web)"""
    video_info = get_video_from_db(video_id)
    if not video_info:
        return jsonify({'error': 'Video no encontrado'}), 404
    
    phone = session.get('phone')
    if not phone:
        return jsonify({'error': 'Sesión no disponible. Por favor, inicia sesión.'}), 401
    
    try:
        client = get_or_create_client(phone)
        if not client or not client.is_connected():
            return jsonify({'error': 'No se pudo conectar a Telegram'}), 500
        
        client_loop = client._loop
        if not client_loop or client_loop.is_closed():
            return jsonify({'error': 'Error de conexión'}), 500
        
        async def get_thumbnail():
            chat_id = video_info.get('chat_id', 'me')
            message_id = video_info['message_id']
            target_chat = int(chat_id) if chat_id != 'me' and str(chat_id).isdigit() else 'me'
            
            messages = await client.get_messages(target_chat, ids=message_id)
            if not messages or not messages.media:
                return None
            
            # Intentar obtener thumbnail del video
            if hasattr(messages.media, 'document'):
                document = messages.media.document
                # Descargar thumbnail usando download_media con thumb=True
                try:
                    thumb_data = await client.download_media(messages, thumb=-1)  # -1 = thumbnail más grande disponible
                    if thumb_data:
                        if isinstance(thumb_data, bytes):
                            return thumb_data
                        elif isinstance(thumb_data, str):
                            # Si es un path, leer el archivo
                            with open(thumb_data, 'rb') as f:
                                return f.read()
                except Exception as e:
                    print(f"⚠️ Error descargando thumbnail: {e}")
                    # Si falla, intentar obtener el primer frame del video
                    # Descargar solo los primeros bytes del video para extraer frame
                    from telethon.tl.types import InputDocumentFileLocation
                    from telethon.tl.functions.upload import GetFileRequest
                    
                    file_location = InputDocumentFileLocation(
                        id=document.id,
                        access_hash=document.access_hash,
                        file_reference=document.file_reference,
                        thumb_size=''
                    )
                    
                    # Descargar primeros 2MB para extraer frame
                    try:
                        thumbnail_limit = get_valid_limit(2 * 1024 * 1024)
                        result = await client(GetFileRequest(
                            location=file_location,
                            offset=0,
                            limit=thumbnail_limit
                        ))
                        if hasattr(result, 'bytes'):
                            return result.bytes[:1024 * 1024]  # Solo primeros 1MB para thumbnail
                    except:
                        pass
            
            return None
        
        try:
            thumbnail_data = run_async(get_thumbnail(), client_loop, timeout=30)
            if thumbnail_data:
                return Response(thumbnail_data, mimetype='image/jpeg')
            else:
                # Si no hay thumbnail, devolver un placeholder
                return jsonify({'error': 'No se pudo obtener la miniatura'}), 404
        except Exception as e:
            print(f"❌ Error obteniendo thumbnail: {e}")
            return jsonify({'error': str(e)}), 500
            
    except Exception as e:
        print(f"❌ Error en get_video_thumbnail: {e}")
        import traceback
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/video/<video_id>')
def get_video(video_id):
    """Obtener el video directamente desde la nube de Telegram (sin caché)"""
    import sys
    import traceback
    
    # Forzar flush de stdout para que los logs aparezcan inmediatamente
    sys.stdout.flush()
    sys.stderr.flush()
    
    try:
        print(f"🎬 Solicitud de video: {video_id}", flush=True)
        print(f"🔍 Request headers: {dict(request.headers)}", flush=True)
        print(f"🔍 Session data: {dict(session)}", flush=True)
        # Verificar si hay range request
        range_header = request.headers.get('Range', None)
        if range_header:
            print(f"📥 Range request: {range_header}", flush=True)
        
        # Verificar conexión a base de datos primero
        try:
            video_info = get_video_from_db(video_id)
        except Exception as db_error:
            error_type = type(db_error).__name__
            error_msg = str(db_error)
            print(f"❌ ERROR DE BASE DE DATOS obteniendo video {video_id}: {error_type}: {error_msg}")
            import traceback
            print(traceback.format_exc())
            return jsonify({
                'error': f'Error de conexión a la base de datos: {error_msg}',
                'error_type': error_type,
                'video_id': video_id,
                'suggestion': 'Verifica que MySQL esté ejecutándose y que la configuración de la base de datos sea correcta.'
            }), 500
        
        if not video_info:
            print(f"❌ Video {video_id} no encontrado en la base de datos")
            return jsonify({'error': 'Video no encontrado'}), 404
        
        print(f"✅ Video encontrado en DB: chat_id={video_info.get('chat_id')}, message_id={video_info.get('message_id')}")
        
        # Obtener phone de la sesión o configuración guardada
        phone = session.get('phone')
        if not phone:
            print(f"❌ No hay sesión activa para video {video_id}")
            print(f"📋 Contenido de session: {list(session.keys())}")
            # Intentar cargar configuración desde archivo como fallback
            try:
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                        saved_config = json.load(f)
                        phone = saved_config.get('phone')
                        if phone:
                            print(f"📱 Usando configuración guardada: {phone}")
                            # Restaurar sesión desde configuración guardada
                            session['phone'] = phone
                            session['api_id'] = saved_config.get('api_id')
                            session['api_hash'] = saved_config.get('api_hash')
                            session['session_name'] = saved_config.get('session_name', f"sessions/{secure_filename(phone)}")
            except Exception as config_error:
                print(f"⚠️ Error cargando configuración guardada: {config_error}")
            
            if not phone:
                return jsonify({
                    'error': 'Sesión no disponible. Por favor, inicia sesión.',
                    'error_type': 'NoSession',
                    'video_id': video_id,
                    'suggestion': 'Por favor, inicia sesión en la aplicación.'
                }), 401
        
        # Verificar que la sesión es válida para este usuario
        if 'phone' not in session:
            return jsonify({'error': 'Sesión no disponible. Por favor, inicia sesión.'}), 401
        
        # Obtener cliente de Telegram
        try:
            print(f"🔌 Intentando obtener cliente de Telegram para {phone}...")
            client = get_or_create_client(phone)
            if not client:
                print(f"❌ No se pudo obtener cliente de Telegram para {phone}")
                return jsonify({
                    'error': 'No se pudo conectar a Telegram',
                    'error_type': 'ClientCreationFailed',
                    'video_id': video_id,
                    'suggestion': 'Intenta recargar la página o iniciar sesión nuevamente.'
                }), 500
            
            # Verificar que el cliente esté conectado
            if not client.is_connected():
                print(f"⚠️ Cliente no conectado, intentando conectar...")
                try:
                    run_async(client.connect(), client._loop, timeout=10)
                    if not client.is_connected():
                        print(f"❌ No se pudo conectar el cliente de Telegram")
                        return jsonify({
                            'error': 'No se pudo conectar a Telegram',
                            'error_type': 'ConnectionFailed',
                            'video_id': video_id,
                            'suggestion': 'Verifica tu conexión a internet e intenta de nuevo.'
                        }), 500
                except Exception as e:
                    error_type = type(e).__name__
                    error_msg = str(e)
                    print(f"❌ Error conectando cliente: {error_type}: {error_msg}")
                    import traceback
                    print(traceback.format_exc())
                    return jsonify({
                        'error': f'Error de conexión: {error_msg}',
                        'error_type': error_type,
                        'video_id': video_id,
                        'suggestion': 'Intenta recargar la página o iniciar sesión nuevamente.'
                    }), 500
            
            # CRÍTICO: Usar SIEMPRE el loop que el cliente tiene asignado internamente
            # NO intentar cambiarlo ni recrearlo - Telethon no lo permite
            client_loop = client._loop
            if not client_loop:
                print(f"❌ Cliente no tiene loop asignado, esto no debería pasar")
                return jsonify({
                    'error': 'Error interno del cliente',
                    'error_type': 'NoEventLoop',
                    'video_id': video_id,
                    'suggestion': 'Por favor, recarga la página e intenta de nuevo.'
                }), 500
            
            # Si el loop está cerrado, intentar recrear el cliente
            if client_loop.is_closed():
                print(f"⚠️ Loop del cliente está cerrado, intentando recrear cliente...")
                try:
                    if phone in telegram_clients:
                        del telegram_clients[phone]
                    client = get_or_create_client(phone)
                    if not client:
                        return jsonify({
                            'error': 'No se pudo recrear el cliente de Telegram',
                            'error_type': 'ClientRecreationFailed',
                            'video_id': video_id,
                            'suggestion': 'Por favor, recarga la página e intenta de nuevo.'
                        }), 500
                    client_loop = client._loop
                    if not client_loop or client_loop.is_closed():
                        return jsonify({
                            'error': 'Error de conexión. Por favor, recarga la página.',
                            'error_type': 'EventLoopClosed',
                            'video_id': video_id,
                            'suggestion': 'Por favor, recarga la página e intenta de nuevo.'
                        }), 500
                except Exception as recreate_error:
                    error_type = type(recreate_error).__name__
                    error_msg = str(recreate_error)
                    print(f"❌ Error recreando cliente: {error_type}: {error_msg}")
                    import traceback
                    print(traceback.format_exc())
                    return jsonify({
                        'error': f'Error de conexión: {error_msg}',
                        'error_type': error_type,
                        'video_id': video_id,
                        'suggestion': 'Por favor, recarga la página e intenta de nuevo.'
                    }), 500
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            print(f"❌ Error obteniendo cliente: {error_type}: {error_msg}")
            import traceback
            traceback_str = traceback.format_exc()
            print(traceback_str)
            return jsonify({
                'error': f'Error obteniendo cliente: {error_msg}',
                'error_type': error_type,
                'video_id': video_id,
                'suggestion': 'Intenta recargar la página o iniciar sesión nuevamente.'
            }), 500
        
        # Obtener chat_id y message_id para usar en las funciones anidadas
        chat_id = video_info.get('chat_id', 'me')
        message_id = video_info['message_id']
        target_chat = int(chat_id) if chat_id != 'me' and str(chat_id).isdigit() else 'me'
        
        # Obtener información del mensaje y el archivo
        async def get_video_info():
            print(f"🔍 Obteniendo mensaje {message_id} del chat {target_chat}...")
            
            # Intentar obtener el mensaje específico
            messages = await client.get_messages(target_chat, ids=message_id)
            
            # Si no se encuentra, buscar en los mensajes recientes (como Telegram hace)
            if not messages:
                print(f"⚠️ Mensaje {message_id} no encontrado directamente, buscando en mensajes recientes...")
                try:
                    # Buscar en los últimos 100 mensajes del chat
                    async for message in client.iter_messages(target_chat, limit=100):
                        if message.id == message_id and message.media:
                            messages = message
                            print(f"✅ Mensaje {message_id} encontrado en búsqueda reciente")
                            break
                except Exception as e:
                    print(f"⚠️ Error buscando mensaje: {e}")
            
            if not messages:
                print(f"⚠️ Mensaje {message_id} no encontrado en chat {target_chat}")
                return None, None, None
            
            if not messages.media:
                print(f"⚠️ Mensaje {message_id} no tiene media")
                return None, None, None
            
            print(f"✅ Mensaje {message_id} obtenido, tiene media: {type(messages.media).__name__}")
            
            # Obtener el documento del mensaje
            if hasattr(messages.media, 'document'):
                document = messages.media.document
                file_size = document.size
                # Obtener el mime_type real del documento
                mime_type = 'video/mp4'  # Fallback por defecto
                if hasattr(document, 'mime_type') and document.mime_type:
                    mime_type = document.mime_type
                    # Asegurarse de que es un tipo de video válido
                    if not mime_type.startswith('video/'):
                        # Si no es video, intentar detectar por extensión del nombre
                        if hasattr(document, 'attributes'):
                            for attr in document.attributes:
                                if hasattr(attr, 'file_name') and attr.file_name:
                                    filename = attr.file_name.lower()
                                    if filename.endswith('.mp4'):
                                        mime_type = 'video/mp4'
                                    elif filename.endswith('.webm'):
                                        mime_type = 'video/webm'
                                    elif filename.endswith('.mkv'):
                                        mime_type = 'video/x-matroska'
                                    elif filename.endswith('.avi'):
                                        mime_type = 'video/x-msvideo'
                                    break
                        # Si aún no es video, usar mp4 como fallback
                        if not mime_type.startswith('video/'):
                            mime_type = 'video/mp4'
                
                # Actualizar el message_id en la base de datos si cambió (por si Telegram lo actualizó)
                if messages.id != message_id:
                    print(f"⚠️ Message ID cambió: {message_id} -> {messages.id}, actualizando DB...")
                    try:
                        with get_db_connection() as conn:
                            with conn.cursor() as cursor:
                                cursor.execute(
                                    "UPDATE videos SET message_id = %s WHERE video_id = %s",
                                    (messages.id, video_id)
                                )
                                conn.commit()
                        print(f"✅ Message ID actualizado en DB")
                    except Exception as e:
                        print(f"⚠️ Error actualizando message_id: {e}")
                
                return messages, file_size, mime_type
            
            return None, None, None
        
        # Aumentar timeout para obtener información del video (60 segundos)
        try:
            messages, file_size, mime_type = run_async(get_video_info(), client_loop, timeout=60)
        except asyncio.TimeoutError:
            print(f"⏱️ Timeout al obtener información del video {video_id} desde Telegram")
            return jsonify({'error': 'Tiempo de espera agotado al obtener el video. Intenta más tarde.'}), 504
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            import traceback
            traceback_str = traceback.format_exc()
            print(f"❌ Error al obtener información del video {video_id}: {error_type}: {error_msg}")
            print(traceback_str)
            
            # Devolver un mensaje de error más descriptivo
            error_response = {
                'error': f'Error al obtener información del video: {error_msg}',
                'error_type': error_type,
                'video_id': video_id
            }
            
            # Si es un error de event loop, sugerir recargar
            if 'event loop' in error_msg.lower() or 'asyncio' in error_msg.lower():
                error_response['suggestion'] = 'Por favor, recarga la página e intenta de nuevo.'
            
            return jsonify(error_response), 500
        
        if not messages:
            print(f"⚠️ No se pudo obtener el mensaje del video {video_id} desde Telegram")
            return jsonify({'error': 'No se pudo obtener el video desde Telegram'}), 500
        
        # Si no hay mime_type, usar mp4 como fallback
        if not mime_type:
            mime_type = 'video/mp4'
        
        print(f"🎬 Streaming video: {mime_type}, tamaño: {file_size} bytes, video_id: {video_id}")
        
        # Headers base para todas las respuestas
        base_headers = {
            'Content-Type': mime_type,
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
            'Access-Control-Allow-Headers': 'Range',
        }
        
        # Si hay range request, servir solo ese rango (streaming progresivo)
        if range_header:
            try:
                range_match = range_header.replace('bytes=', '').split('-')
                start = int(range_match[0]) if range_match[0] else 0
                end = int(range_match[1]) if range_match[1] else file_size - 1
                
                if start < 0:
                    start = 0
                if end >= file_size:
                    end = file_size - 1
                if start > end:
                    start = end
                
                chunk_size = end - start + 1
                
                # Validar chunk_size
                if chunk_size <= 0:
                    print(f"⚠️ chunk_size inválido: {chunk_size}, usando 1MB por defecto")
                    chunk_size = 1024 * 1024
                
                print(f"📊 Range request: start={start}, end={end}, chunk_size={chunk_size}, file_size={file_size}")
                
                # Descargar solo el rango solicitado usando GetFileRequest (streaming progresivo real)
                async def download_range():
                    # Obtener el InputFileLocation del documento
                    if hasattr(messages.media, 'document'):
                        document = messages.media.document
                        # Obtener el InputDocumentFileLocation
                        from telethon.tl.types import InputDocumentFileLocation
                        from telethon.tl.functions.upload import GetFileRequest
                        
                        file_location = InputDocumentFileLocation(
                            id=document.id,
                            access_hash=document.access_hash,
                            file_reference=document.file_reference,
                            thumb_size=''
                        )
                        
                        # Asegurarse de que file_reference esté actualizado
                        if not document.file_reference:
                            print(f"⚠️ file_reference vacío en range request, intentando actualizar...")
                            try:
                                updated_messages = await client.get_messages(target_chat, ids=message_id)
                                if updated_messages and hasattr(updated_messages.media, 'document'):
                                    document = updated_messages.media.document
                                    file_location = InputDocumentFileLocation(
                                        id=document.id,
                                        access_hash=document.access_hash,
                                        file_reference=document.file_reference,
                                        thumb_size=''
                                    )
                                    print(f"✅ file_reference actualizado en range request")
                            except Exception as ref_error:
                                print(f"⚠️ Error actualizando file_reference en range: {ref_error}")
                        
                        # Descargar SOLO el rango solicitado usando GetFileRequest
                        # Esto permite streaming progresivo real - el video empieza a reproducirse inmediatamente
                        try:
                            # Calcular el tamaño restante del archivo desde el offset
                            remaining_size = file_size - start
                            
                            # Si estamos cerca del final del archivo (último 10%), usar chunks más pequeños
                            # Telegram puede tener problemas con offsets grandes y limits grandes
                            file_progress = (start / file_size) if file_size > 0 else 0
                            is_near_end = file_progress > 0.9
                            
                            # Ajustar el máximo permitido según la posición en el archivo
                            if is_near_end:
                                # Cerca del final: usar máximo 512KB para evitar problemas
                                max_limit = 512 * 1024  # 512KB
                                print(f"⚠️ Cerca del final del archivo ({file_progress*100:.1f}%), usando chunks más pequeños (512KB)", flush=True)
                            else:
                                max_limit = 1024 * 1024  # 1MB máximo
                            
                            # El limit debe ser el mínimo entre:
                            # 1. El chunk_size solicitado
                            # 2. El tamaño restante del archivo (CRÍTICO: nunca exceder)
                            # 3. El máximo permitido según la posición
                            requested_limit = min(chunk_size, remaining_size, max_limit)
                            
                            # CRÍTICO: Usar la función get_valid_limit con max_allowed=remaining_size
                            # Esto asegura que el limit nunca exceda remaining_size
                            valid_limit = get_valid_limit(requested_limit, max_allowed=remaining_size)
                            
                            # Verificación final: asegurar que nunca exceda remaining_size
                            if valid_limit > remaining_size:
                                print(f"⚠️ ERROR CRÍTICO: valid_limit ({valid_limit}) > remaining_size ({remaining_size}), usando remaining_size exacto", flush=True)
                                # Si remaining_size es menor que 1024, usar exactamente remaining_size
                                if remaining_size < 1024 and remaining_size > 0:
                                    valid_limit = remaining_size
                                else:
                                    # Redondear hacia abajo al múltiplo de 1024 más cercano
                                    valid_limit = (remaining_size // 1024) * 1024
                                    if valid_limit < 1024 and remaining_size >= 1024:
                                        valid_limit = 1024
                            
                            print(f"🔍 Intentando GetFileRequest range: offset={start}, limit={valid_limit} (solicitado: {chunk_size}, remaining: {remaining_size}, file_size: {file_size}, progress: {file_progress*100:.1f}%), file_id={document.id}, limit%1024={valid_limit % 1024}, es_multiplo_1024={valid_limit % 1024 == 0}", flush=True)
                            result = await client(GetFileRequest(
                                location=file_location,
                                offset=start,
                                limit=valid_limit
                            ))
                            
                            # El resultado de GetFileRequest puede tener diferentes estructuras
                            # Intentamos obtener los bytes de diferentes maneras
                            data = None
                            if hasattr(result, 'bytes'):
                                data = result.bytes
                            elif hasattr(result, 'data'):
                                data = result.data
                            elif isinstance(result, bytes):
                                data = result
                            elif hasattr(result, '__bytes__'):
                                data = bytes(result)
                            
                            if data and len(data) > 0:
                                print(f"✅ GetFileRequest range exitoso: {len(data)} bytes descargados (rango {start}-{start+len(data)-1})")
                                return data
                            else:
                                # Fallback: intentar con chunks más pequeños si el chunk grande falla
                                # Para videos muy grandes, dividir en chunks de 1MB
                                print(f"⚠️ GetFileRequest no devolvió bytes válidos, intentando con chunks más pequeños")
                                chunk_limit = min(1024 * 1024, chunk_size)  # Máximo 1MB por chunk
                                buffer = BytesIO()
                                current_offset = start
                                remaining = chunk_size
                                
                                while remaining > 0:
                                    current_chunk_size = min(chunk_limit, remaining)
                                    # Asegurar que el limit sea múltiplo de 1024 y no exceda remaining
                                    valid_chunk_limit = get_valid_limit(current_chunk_size)
                                    
                                    # CRÍTICO: Asegurar que no exceda remaining
                                    if valid_chunk_limit > remaining:
                                        valid_chunk_limit = (remaining // 1024) * 1024
                                        if valid_chunk_limit < 1024 and remaining > 0:
                                            valid_chunk_limit = remaining
                                        elif valid_chunk_limit < 1024:
                                            valid_chunk_limit = 1024
                                    
                                    try:
                                        result = await client(GetFileRequest(
                                            location=file_location,
                                            offset=current_offset,
                                            limit=valid_chunk_limit
                                        ))
                                        chunk_data = None
                                        if hasattr(result, 'bytes'):
                                            chunk_data = result.bytes
                                        elif hasattr(result, 'data'):
                                            chunk_data = result.data
                                        elif isinstance(result, bytes):
                                            chunk_data = result
                                        
                                        if chunk_data and len(chunk_data) > 0:
                                            buffer.write(chunk_data)
                                            current_offset += len(chunk_data)
                                            remaining -= len(chunk_data)
                                        else:
                                            print(f"⚠️ Chunk en offset {current_offset} no devolvió datos")
                                            break
                                    except Exception as chunk_error:
                                        print(f"⚠️ Error descargando chunk en offset {current_offset}: {chunk_error}")
                                        break
                                
                                if buffer.tell() > 0:
                                    buffer.seek(0)
                                    return buffer.read()
                                else:
                                    raise Exception("No se pudo descargar ningún chunk del rango solicitado")
                        except Exception as e:
                            error_msg = str(e)
                            error_type = type(e).__name__
                            print(f"❌ Error con GetFileRequest en range: {error_type}: {error_msg}")
                            import traceback
                            traceback_str = traceback.format_exc()
                            print(traceback_str)
                            
                            # Si el error es de file_reference obsoleto, intentar actualizarlo
                            if 'file_reference' in error_msg.lower() or 'FILE_REFERENCE' in str(e):
                                print(f"🔄 Error de file_reference en range, intentando actualizar mensaje...")
                                try:
                                    updated_messages = await client.get_messages(target_chat, ids=message_id)
                                    if updated_messages and hasattr(updated_messages.media, 'document'):
                                        document = updated_messages.media.document
                                        file_location = InputDocumentFileLocation(
                                            id=document.id,
                                            access_hash=document.access_hash,
                                            file_reference=document.file_reference,
                                            thumb_size=''
                                        )
                                        print(f"✅ file_reference actualizado en range, reintentando GetFileRequest...")
                                        # Reintentar con file_reference actualizado
                                        retry_limit = get_valid_limit(min(1024 * 1024, chunk_size))
                                        result = await client(GetFileRequest(
                                            location=file_location,
                                            offset=start,
                                            limit=retry_limit
                                        ))
                                        data = None
                                        if hasattr(result, 'bytes'):
                                            data = result.bytes
                                        elif hasattr(result, 'data'):
                                            data = result.data
                                        elif isinstance(result, bytes):
                                            data = result
                                        
                                        if data and len(data) > 0:
                                            print(f"✅ GetFileRequest range exitoso después de actualizar file_reference: {len(data)} bytes")
                                            return data
                                except Exception as retry_error:
                                    print(f"⚠️ Error en reintento con file_reference actualizado en range: {retry_error}")
                            
                            # Para videos muy grandes, NO intentar descargar todo - solo lanzar error descriptivo
                            if file_size > 1024 * 1024 * 1024:  # > 1GB
                                raise Exception(f"No se pudo descargar el rango del video. El video es muy grande ({file_size / (1024*1024*1024):.2f}GB) y requiere streaming progresivo. Error: {error_type}: {error_msg}")
                            else:
                                raise Exception(f"Error descargando rango del video: {error_type}: {error_msg}")
                    return None
                
                # Timeout dinámico (más corto porque solo descargamos un chunk)
                timeout_seconds = min(60, max(10, int(chunk_size / (1024 * 1024)) * 5))
                
                chunk_data = run_async(download_range(), client_loop, timeout=timeout_seconds)
                
                if chunk_data and len(chunk_data) > 0:
                    headers = {
                        **base_headers,
                        'Content-Range': f'bytes {start}-{start+len(chunk_data)-1}/{file_size}',
                        'Content-Length': str(len(chunk_data)),
                    }
                    return Response(chunk_data, 206, headers, mimetype=mime_type)
                else:
                    return jsonify({'error': 'No se pudo descargar el rango del video'}), 500
                    
            except Exception as e:
                print(f"⚠️ Error con range request: {e}")
                import traceback
                print(traceback.format_exc())
                return jsonify({'error': str(e)}), 500
        
        # Si no hay range request, servir los primeros bytes para metadata
        # El navegador luego hará range requests para el resto
        print(f"📥 Solicitud inicial, sirviendo primeros bytes para metadata...")
        
        async def download_initial_chunk():
            # Para videos pesados, necesitamos un chunk inicial más grande para que el navegador
            # tenga suficiente información para empezar a reproducir mientras descarga el resto
            # Para videos de 4GB+, usamos chunks más grandes para mejor experiencia
            if file_size > 2 * 1024 * 1024 * 1024:  # Videos > 2GB (muy pesados)
                initial_size = min(20 * 1024 * 1024, file_size)  # 20MB para videos muy grandes
            elif file_size > 500 * 1024 * 1024:  # Videos > 500MB
                initial_size = min(10 * 1024 * 1024, file_size)  # 10MB para videos grandes
            elif file_size > 50 * 1024 * 1024:  # Videos > 50MB
                initial_size = min(5 * 1024 * 1024, file_size)  # 5MB
            elif file_size > 10 * 1024 * 1024:  # Videos > 10MB
                initial_size = min(3 * 1024 * 1024, file_size)  # 3MB
            else:
                initial_size = min(1024 * 1024, file_size)  # 1MB
            
            print(f"📊 Descargando chunk inicial de {initial_size / (1024*1024):.2f}MB para video de {file_size / (1024*1024):.2f}MB ({file_size / (1024*1024*1024):.2f}GB)")
            
            # Usar GetFileRequest para descargar solo los primeros bytes (MUCHO más rápido)
            if hasattr(messages.media, 'document'):
                document = messages.media.document
                from telethon.tl.types import InputDocumentFileLocation
                from telethon.tl.functions.upload import GetFileRequest
                
                file_location = InputDocumentFileLocation(
                    id=document.id,
                    access_hash=document.access_hash,
                    file_reference=document.file_reference,
                    thumb_size=''
                )
                
                # Descargar SOLO los primeros bytes usando GetFileRequest (streaming progresivo)
                try:
                    # Asegurarse de que file_reference esté actualizado
                    if not document.file_reference:
                        print(f"⚠️ file_reference vacío, intentando actualizar...")
                        # Intentar obtener el mensaje de nuevo para actualizar file_reference
                        try:
                            updated_messages = await client.get_messages(target_chat, ids=message_id)
                            if updated_messages and hasattr(updated_messages.media, 'document'):
                                document = updated_messages.media.document
                                file_location = InputDocumentFileLocation(
                                    id=document.id,
                                    access_hash=document.access_hash,
                                    file_reference=document.file_reference,
                                    thumb_size=''
                                )
                                print(f"✅ file_reference actualizado")
                        except Exception as ref_error:
                            print(f"⚠️ Error actualizando file_reference: {ref_error}")
                    
                    # Calcular limit válido para el chunk inicial
                    # CRÍTICO: El limit no puede exceder el tamaño del archivo
                    max_allowed_limit = min(initial_size, file_size)
                    valid_initial_limit = get_valid_limit(max_allowed_limit)
                    
                    # Verificación final: asegurar que no exceda file_size
                    if valid_initial_limit > file_size:
                        valid_initial_limit = (file_size // 1024) * 1024
                        if valid_initial_limit < 1024 and file_size > 0:
                            valid_initial_limit = file_size
                        elif valid_initial_limit < 1024:
                            valid_initial_limit = 1024
                    
                    # Verificación final: asegurar que sea múltiplo de 1024 (o el tamaño exacto si es menor)
                    if valid_initial_limit >= 1024 and valid_initial_limit % 1024 != 0:
                        valid_initial_limit = (valid_initial_limit // 1024) * 1024
                        if valid_initial_limit < 1024:
                            valid_initial_limit = 1024
                    
                    print(f"🔍 Intentando GetFileRequest: offset=0, limit={valid_initial_limit} (solicitado: {initial_size}, file_size: {file_size}), file_id={document.id}, limit%1024={valid_initial_limit % 1024 if valid_initial_limit >= 1024 else 'N/A'}, es_multiplo_1024={valid_initial_limit % 1024 == 0 if valid_initial_limit >= 1024 else True}", flush=True)
                    result = await client(GetFileRequest(
                        location=file_location,
                        offset=0,
                        limit=valid_initial_limit
                    ))
                    
                    print(f"🔍 Resultado de GetFileRequest: tipo={type(result).__name__}, atributos={dir(result)}")
                    
                    # El resultado de GetFileRequest puede tener diferentes estructuras
                    # Intentamos obtener los bytes de diferentes maneras
                    data = None
                    if hasattr(result, 'bytes'):
                        data = result.bytes
                        print(f"✅ Datos obtenidos de result.bytes: {len(data) if data else 0} bytes")
                    elif hasattr(result, 'data'):
                        data = result.data
                        print(f"✅ Datos obtenidos de result.data: {len(data) if data else 0} bytes")
                    elif isinstance(result, bytes):
                        data = result
                        print(f"✅ Datos obtenidos directamente como bytes: {len(data)} bytes")
                    elif hasattr(result, '__bytes__'):
                        data = bytes(result)
                        print(f"✅ Datos obtenidos de __bytes__: {len(data)} bytes")
                    else:
                        print(f"⚠️ Resultado de GetFileRequest no tiene estructura conocida: {result}")
                    
                    if data and len(data) > 0:
                        print(f"✅ GetFileRequest exitoso: {len(data)} bytes descargados")
                        return data
                    else:
                        # Fallback: intentar con chunks más pequeños si el chunk grande falla
                        print(f"⚠️ GetFileRequest no devolvió bytes válidos, intentando con chunks más pequeños para chunk inicial")
                        chunk_limit = min(1024 * 1024, initial_size)  # Máximo 1MB por chunk
                        buffer = BytesIO()
                        current_offset = 0
                        remaining = initial_size
                        
                        while remaining > 0:
                            current_chunk_size = min(chunk_limit, remaining)
                            # Asegurar que el limit sea múltiplo de 1024 y no exceda remaining
                            valid_chunk_limit = get_valid_limit(current_chunk_size)
                            
                            # CRÍTICO: Asegurar que no exceda remaining
                            if valid_chunk_limit > remaining:
                                valid_chunk_limit = (remaining // 1024) * 1024
                                if valid_chunk_limit < 1024 and remaining > 0:
                                    valid_chunk_limit = remaining
                                elif valid_chunk_limit < 1024:
                                    valid_chunk_limit = 1024
                            
                            try:
                                result = await client(GetFileRequest(
                                    location=file_location,
                                    offset=current_offset,
                                    limit=valid_chunk_limit
                                ))
                                chunk_data = None
                                if hasattr(result, 'bytes'):
                                    chunk_data = result.bytes
                                elif hasattr(result, 'data'):
                                    chunk_data = result.data
                                elif isinstance(result, bytes):
                                    chunk_data = result
                                
                                if chunk_data and len(chunk_data) > 0:
                                    buffer.write(chunk_data)
                                    current_offset += len(chunk_data)
                                    remaining -= len(chunk_data)
                                else:
                                    print(f"⚠️ Chunk inicial en offset {current_offset} no devolvió datos")
                                    break
                            except Exception as chunk_error:
                                print(f"⚠️ Error descargando chunk inicial en offset {current_offset}: {chunk_error}")
                                break
                        
                        if buffer.tell() > 0:
                            buffer.seek(0)
                            return buffer.read()
                        else:
                            raise Exception("No se pudo descargar el chunk inicial del video")
                except Exception as e:
                    error_msg = str(e)
                    error_type = type(e).__name__
                    print(f"❌ Error con GetFileRequest en chunk inicial: {error_type}: {error_msg}")
                    import traceback
                    traceback_str = traceback.format_exc()
                    print(traceback_str)
                    
                    # Si el error es de file_reference obsoleto, intentar actualizarlo
                    if 'file_reference' in error_msg.lower() or 'FILE_REFERENCE' in str(e):
                        print(f"🔄 Error de file_reference, intentando actualizar mensaje...")
                        try:
                            updated_messages = await client.get_messages(target_chat, ids=message_id)
                            if updated_messages and hasattr(updated_messages.media, 'document'):
                                document = updated_messages.media.document
                                file_location = InputDocumentFileLocation(
                                    id=document.id,
                                    access_hash=document.access_hash,
                                    file_reference=document.file_reference,
                                    thumb_size=''
                                )
                                print(f"✅ file_reference actualizado, reintentando GetFileRequest...")
                                # Reintentar con file_reference actualizado
                                retry_limit = get_valid_limit(min(1024 * 1024, initial_size))
                                result = await client(GetFileRequest(
                                    location=file_location,
                                    offset=0,
                                    limit=retry_limit
                                ))
                                data = None
                                if hasattr(result, 'bytes'):
                                    data = result.bytes
                                elif hasattr(result, 'data'):
                                    data = result.data
                                elif isinstance(result, bytes):
                                    data = result
                                
                                if data and len(data) > 0:
                                    print(f"✅ GetFileRequest exitoso después de actualizar file_reference: {len(data)} bytes")
                                    return data
                        except Exception as retry_error:
                            print(f"⚠️ Error en reintento con file_reference actualizado: {retry_error}")
                    
                    # No hay fallback viable - GetFileRequest es la única forma de streaming progresivo
                    # Si falla, el error ya fue manejado arriba con reintentos de file_reference
                    # Para videos muy grandes, NO intentar descargar todo - solo lanzar error descriptivo
                    if file_size > 1024 * 1024 * 1024:  # > 1GB
                        raise Exception(f"No se pudo descargar el chunk inicial del video. El video es muy grande ({file_size / (1024*1024*1024):.2f}GB) y requiere streaming progresivo. Error: {error_type}: {error_msg}")
                    else:
                        raise Exception(f"Error descargando chunk inicial: {error_type}: {error_msg}")
            
            # Si llegamos aquí sin retornar, algo salió mal
            raise Exception("No se pudo descargar el chunk inicial del video: función retornó None")
        
        # Timeout dinámico basado en el tamaño del video
        # Para videos muy grandes (4GB+), necesitamos más tiempo
        if file_size > 2 * 1024 * 1024 * 1024:  # > 2GB
            timeout_initial = 180  # 3 minutos para videos muy grandes
        elif file_size > 500 * 1024 * 1024:  # > 500MB
            timeout_initial = 120  # 2 minutos
        else:
            timeout_initial = 60  # 1 minuto para videos normales
        
        try:
            # Verificar que el loop esté disponible antes de descargar
            if not client_loop or client_loop.is_closed():
                print(f"❌ Loop no disponible antes de download_initial_chunk, intentando recrear cliente...")
                if phone in telegram_clients:
                    del telegram_clients[phone]
                client = get_or_create_client(phone)
                if not client:
                    return jsonify({'error': 'No se pudo obtener un cliente válido'}), 500
                client_loop = client._loop
                if not client_loop or client_loop.is_closed():
                    return jsonify({'error': 'No se pudo obtener un loop válido'}), 500
            
            print(f"⏱️ Timeout configurado: {timeout_initial}s para video de {file_size / (1024*1024*1024):.2f}GB")
            try:
                initial_data = run_async(download_initial_chunk(), client_loop, timeout=timeout_initial)
            except asyncio.TimeoutError:
                print(f"⏱️ Timeout al descargar chunk inicial del video {video_id}")
                return jsonify({'error': 'Tiempo de espera agotado al descargar el video. Intenta más tarde.'}), 504
            except Exception as run_error:
                error_type = type(run_error).__name__
                error_msg = str(run_error)
                print(f"❌ Error en run_async para chunk inicial: {error_type}: {error_msg}")
                import traceback
                print(traceback.format_exc())
                return jsonify({
                    'error': f'Error al descargar el video: {error_msg}',
                    'error_type': error_type,
                    'video_id': video_id
                }), 500
            
            if initial_data:
                headers = {
                    **base_headers,
                    'Content-Length': str(file_size),
                    'Content-Range': f'bytes 0-{len(initial_data)-1}/{file_size}',
                }
                # Si el archivo es pequeño, servir completo
                if len(initial_data) >= file_size:
                    return Response(initial_data, 200, headers, mimetype=mime_type)
                else:
                    # Servir solo los primeros bytes, el navegador hará range requests
                    return Response(initial_data, 206, headers, mimetype=mime_type)
            else:
                print(f"❌ download_initial_chunk retornó None para video {video_id}")
                return jsonify({'error': 'No se pudo obtener el video'}), 500
        except Exception as e:
            import traceback
            error_type = type(e).__name__
            error_msg = str(e)
            traceback_str = traceback.format_exc()
            print(f"❌ Error descargando chunk inicial del video {video_id}: {error_type}: {error_msg}")
            print(traceback_str)
            
            # Devolver un mensaje de error más descriptivo
            error_response = {
                'error': f'Error al descargar el video: {error_msg}',
                'error_type': error_type,
                'video_id': video_id
            }
            
            # Si es un error de event loop o file_reference, sugerir recargar
            if 'event loop' in error_msg.lower() or 'asyncio' in error_msg.lower() or 'file_reference' in error_msg.lower():
                error_response['suggestion'] = 'Por favor, recarga la página e intenta de nuevo.'
            
            return jsonify(error_response), 500
            
    except Exception as e:
        import sys
        import traceback
        error_type = type(e).__name__
        error_msg = str(e)
        traceback_str = traceback.format_exc()
        print(f"❌ ERROR GENERAL obteniendo video {video_id}: {error_type}: {error_msg}", flush=True)
        print(f"📍 Traceback completo:", flush=True)
        print(traceback_str, flush=True)
        # También escribir a stderr para asegurar que se vea
        sys.stderr.write(f"❌ ERROR GENERAL obteniendo video {video_id}: {error_type}: {error_msg}\n")
        sys.stderr.write(f"📍 Traceback completo:\n{traceback_str}\n")
        sys.stderr.flush()
        
        # Información adicional para debugging
        debug_info = {
            'video_id': video_id,
            'has_session': 'phone' in session,
            'session_keys': list(session.keys()) if session else [],
            'range_header': request.headers.get('Range', None),
            'user_agent': request.headers.get('User-Agent', 'Unknown')[:100]
        }
        print(f"🔍 Debug info: {debug_info}")
        
        # Devolver un mensaje de error más descriptivo
        error_response = {
            'error': f'Error al obtener el video: {error_msg}',
            'error_type': error_type,
            'video_id': video_id,
            'debug_info': debug_info  # Incluir info de debug en desarrollo
        }
        
        # Si es un error de event loop, sugerir recargar
        if 'event loop' in error_msg.lower() or 'asyncio' in error_msg.lower():
            error_response['suggestion'] = 'Por favor, recarga la página e intenta de nuevo.'
        elif 'session' in error_msg.lower() or 'phone' in error_msg.lower():
            error_response['suggestion'] = 'Por favor, inicia sesión nuevamente.'
        elif 'database' in error_msg.lower() or 'mysql' in error_msg.lower():
            error_response['suggestion'] = 'Verifica que MySQL esté ejecutándose correctamente.'
        
        return jsonify(error_response), 500

@app.route('/api/debug/video/<video_id>', methods=['GET'])
def debug_video(video_id):
    """Endpoint de diagnóstico para verificar el estado de un video"""
    debug_info = {
        'video_id': video_id,
        'session': {
            'has_phone': 'phone' in session,
            'phone': session.get('phone', None),
            'keys': list(session.keys()),
            'has_api_id': 'api_id' in session,
            'has_api_hash': 'api_hash' in session,
        },
        'database': {
            'config_exists': db_config is not None,
            'config': {k: v if k != 'password' else '***' for k, v in db_config.items()} if db_config else None
        },
        'video_in_db': None,
        'telegram_client': {
            'available': False,
            'connected': False
        }
    }
    
    # Intentar obtener video de la base de datos
    try:
        video_info = get_video_from_db(video_id)
        debug_info['video_in_db'] = video_info is not None
        if video_info:
            debug_info['video_details'] = {
                'chat_id': video_info.get('chat_id'),
                'message_id': video_info.get('message_id'),
                'filename': video_info.get('filename')
            }
    except Exception as e:
        debug_info['video_in_db'] = False
        debug_info['db_error'] = str(e)
    
    # Verificar cliente de Telegram
    phone = session.get('phone')
    if phone and phone in telegram_clients:
        debug_info['telegram_client']['available'] = True
        client_data = telegram_clients[phone]
        if 'client' in client_data:
            client = client_data['client']
            try:
                debug_info['telegram_client']['connected'] = client.is_connected()
            except:
                pass
    
    return jsonify(debug_info)

@app.route('/api/logout', methods=['POST'])
def logout():
    """Cerrar sesión"""
    phone = session.get('phone')
    
    # Desconectar cliente si existe
    if phone and phone in telegram_clients:
        client_data = telegram_clients[phone]
        client = client_data.get('client')
        if client and client.is_connected():
            try:
                run_async(client.disconnect())
            except:
                pass
        del telegram_clients[phone]
    
    # Limpiar sesión
    session.clear()
    
    # NO eliminar configuración global - cada usuario tiene su propia sesión
    # La sesión de Flask se limpia automáticamente al hacer session.clear()
    
    return jsonify({'message': 'Sesión cerrada exitosamente', 'redirect': '/'})

@app.template_filter('timestamp_to_date')
def timestamp_to_date(timestamp):
    """Convertir timestamp a fecha legible"""
    return datetime.fromtimestamp(timestamp).strftime('%d/%m/%Y %H:%M')

@app.route('/videos')
def list_videos():
    """Listar todos los videos subidos"""
    videos = []
    all_videos = get_all_videos_from_db()
    for video_id, info in all_videos.items():
        videos.append({
            'id': video_id,
            'filename': info['filename'],
            'timestamp': info['timestamp'],
            'view_url': f'/watch/{video_id}'
        })
    
    # Ordenar por timestamp descendente (más recientes primero)
    videos.sort(key=lambda x: x['timestamp'], reverse=True)
    
    return render_template('list.html', videos=videos)

@app.route('/api/cleanup', methods=['POST'])
def cleanup_uploads():
    """Limpiar archivos antiguos de uploads manualmente"""
    try:
        cleanup_old_uploads()
        return jsonify({'message': 'Limpieza completada'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Manejador de errores para asegurar que todas las rutas API devuelvan JSON
@app.errorhandler(404)
def not_found(error):
    """Manejar errores 404 - devolver JSON si es una ruta API"""
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Endpoint no encontrado'}), 404
    return render_template('index.html'), 404

@app.errorhandler(500)
def internal_error(error):
    """Manejar errores 500 - devolver JSON si es una ruta API"""
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Error interno del servidor'}), 500
    return render_template('index.html'), 500

@app.errorhandler(401)
def unauthorized(error):
    """Manejar errores 401 - devolver JSON si es una ruta API"""
    if request.path.startswith('/api/'):
        return jsonify({'error': 'No autorizado. Por favor, inicia sesión.'}), 401
    return redirect(url_for('index')), 401

if __name__ == '__main__':
    # En producción, usar debug=False
    import os
    import sys
    
    # Configurar logging para que todo se vea en stdout/stderr
    import logging
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.StreamHandler(sys.stderr)
        ]
    )
    
    # Forzar que print() siempre haga flush
    import functools
    original_print = print
    def print_with_flush(*args, **kwargs):
        kwargs.setdefault('flush', True)
        original_print(*args, **kwargs)
    print = print_with_flush
    
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)

