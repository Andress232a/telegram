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
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max

# Crear carpetas necesarias
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('sessions', exist_ok=True)
# NO crear video_cache_temp - todo se sirve directamente desde la nube de Telegram

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
        raise Exception("Configuración de base de datos no disponible")
    
    conn = None
    try:
        conn = pymysql.connect(
            host=db_config['host'],
            user=db_config['user'],
            password=db_config['password'],
            database=db_config['database'],
            charset=db_config.get('charset', 'utf8mb4'),
            cursorclass=pymysql.cursors.DictCursor
        )
        yield conn
    except Exception as e:
        print(f"❌ Error de conexión a MySQL: {e}")
        raise
    finally:
        if conn:
            conn.close()

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
    except Exception as e:
        print(f"❌ Error obteniendo video desde DB: {e}")
        return None

def find_video_by_message(chat_id, message_id, phone):
    """Buscar video existente por chat_id, message_id y phone"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Buscar por chat_id y message_id (phone no está en la tabla, se maneja en la app)
                cursor.execute(
                    "SELECT video_id FROM videos WHERE chat_id = %s AND message_id = %s ORDER BY created_at DESC LIMIT 1",
                    (str(chat_id), message_id)
                )
                result = cursor.fetchone()
                if result:
                    return result['video_id']
                return None
    except Exception as e:
        print(f"❌ Error buscando video en DB: {e}")
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
                return loop
        
        # Crear nuevo loop para este thread
        loop = asyncio.new_event_loop()
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
        # Asegurarse de que hay un event loop antes de crear el cliente
        print("🔄 Creando event loop...")
        loop = get_event_loop()
        
        # Crear cliente de Telegram (necesita un loop disponible)
        print("📱 Creando cliente de Telegram...")
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
        # Recrear cliente en este thread
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
    """Obtener o crear cliente de forma segura, evitando bloqueos de base de datos"""
    if phone in telegram_clients:
        client_data = telegram_clients[phone]
        client = client_data.get('client')
        loop = client_data.get('loop')
        
        # Verificar que el cliente existe y el loop es válido
        if client:
            # PRIMERO verificar que el loop no esté cerrado
            if loop and not loop.is_closed():
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
    session_name = session.get('session_name', f"sessions/{secure_filename(phone)}")
    api_id = int(session['api_id'])
    api_hash = session['api_hash']
    
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
            print(f"❌ Error conectando cliente en get_or_create_client: {e}")
            import traceback
            print(traceback.format_exc())
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
                                chat_id_str = str(chat_id)
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
                                    
                                    if save_video_to_db(video_id, chat_id_str, message.id, filename, timestamp, file_size):
                                        existing_video_id = video_id
                                        print(f"✅ Video nuevo registrado: Message={message.id}, VideoID={existing_video_id}, Filename={filename}")
                                    else:
                                        print(f"⚠️ Error guardando video en DB, pero continuando...")
                                        existing_video_id = video_id
                                else:
                                    print(f"✅ Video existente encontrado: {existing_video_id} (Message={message.id}, Chat={chat_id_str})")
                                
                                msg_info['video_url'] = f'/api/video/{existing_video_id}'
                                print(f"✅ Video URL asignado: {msg_info['video_url']}")
                                
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
    if 'phone' not in session:
        return jsonify({'error': 'No estás conectado a Telegram'}), 401
    
    chat_id = request.form.get('chat_id', 'me')  # Por defecto a "me" (Saved Messages)
    description = request.form.get('description', '')  # Descripción opcional del video
    
    if 'video' not in request.files:
        return jsonify({'error': 'No se encontró el archivo de video'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'No se seleccionó ningún archivo'}), 400
    
    filename = secure_filename(file.filename)
    timestamp = int(time.time())
    upload_id = secrets.token_urlsafe(8)
    
    # Obtener valores de la sesión ANTES de crear el thread
    phone = session['phone']
    api_id = int(session['api_id'])
    api_hash = session['api_hash']
    session_name = session.get('session_name', f"sessions/{secure_filename(phone)}")
    
    # Inicializar progreso ANTES de guardar el archivo
    upload_progress[upload_id] = {'progress': 0, 'status': 'uploading', 'current': 0, 'total': 0}
    print(f"✅ Upload ID creado: {upload_id}")
    print(f"📋 Upload IDs disponibles después de crear: {list(upload_progress.keys())}")
    
    # Guardar archivo temporalmente en local
    local_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{timestamp}_{filename}")
    file.save(local_path)
    file_size = os.path.getsize(local_path)
    upload_progress[upload_id]['total'] = file_size
    print(f"💾 Archivo guardado temporalmente: {local_path} ({file_size} bytes)")
    print(f"📋 Upload IDs disponibles después de guardar archivo: {list(upload_progress.keys())}")
    
    # Devolver upload_id INMEDIATAMENTE para que el frontend pueda monitorear
    # La subida se ejecutará en segundo plano
    def upload_in_background(phone_param, api_id_param, api_hash_param, session_name_param, chat_id_param, local_path_param, filename_param, upload_id_param, timestamp_param, file_size_param, description_param=''):
        try:
            # Asegurarse de que el upload_id existe ANTES de comenzar la subida
            if upload_id_param not in upload_progress:
                print(f"⚠️ Upload ID {upload_id_param} no encontrado al iniciar background, inicializando...")
                upload_progress[upload_id_param] = {
                    'progress': 0, 
                    'status': 'uploading', 
                    'current': 0, 
                    'total': file_size_param
                }
            else:
                # Asegurarse de que el total esté configurado
                upload_progress[upload_id_param]['total'] = file_size_param
                upload_progress[upload_id_param]['status'] = 'uploading'
            
            print(f"🚀 Iniciando subida en background - Upload ID: {upload_id_param}")
            print(f"📋 Upload IDs disponibles al iniciar background: {list(upload_progress.keys())}")
            
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
            def progress_callback(current, total):
                if total > 0:
                    progress = int((current / total) * 100)
                    # Asegurarse de que el upload_id existe en el diccionario
                    if upload_id_param not in upload_progress:
                        print(f"⚠️ Upload ID {upload_id_param} no encontrado en callback, inicializando...")
                        upload_progress[upload_id_param] = {
                            'progress': 0, 
                            'status': 'uploading', 
                            'current': 0, 
                            'total': total
                        }
                    
                    # Actualizar progreso de forma thread-safe
                    upload_progress[upload_id_param]['progress'] = progress
                    upload_progress[upload_id_param]['current'] = current
                    upload_progress[upload_id_param]['total'] = total
                    upload_progress[upload_id_param]['status'] = 'uploading'
                    
                    # Loggear cada 5% para no saturar
                    if progress % 5 == 0 or progress == 100:
                        print(f"📤 Progreso: {progress}% ({current}/{total} bytes) - Upload ID: {upload_id_param}")
                        print(f"📋 Upload IDs disponibles en callback: {list(upload_progress.keys())}")
            
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
            # Calcular timeout basado en el tamaño del archivo (1 minuto por cada 10MB, mínimo 5 minutos)
            file_size_mb = file_size_param / (1024 * 1024)
            timeout_seconds = max(300, int(file_size_mb * 6))  # Mínimo 5 minutos, 6 segundos por MB
            print(f"⏱️ Timeout configurado: {timeout_seconds} segundos para archivo de {file_size_mb:.2f} MB")
            
            message = run_async(upload(), client_loop, timeout=timeout_seconds)
            
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
            # Marcar como error
            if upload_id_param in upload_progress:
                upload_progress[upload_id_param]['status'] = 'error'
                upload_progress[upload_id_param]['error'] = str(e)
            
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
            
            import traceback
            print(f"❌ Error subiendo video: {e}")
            print(traceback.format_exc())
    
    # Ejecutar subida en segundo plano (pasar valores como parámetros, no usar sesión)
    import threading
    upload_thread = threading.Thread(
        target=upload_in_background, 
        args=(phone, api_id, api_hash, session_name, chat_id, local_path, filename, upload_id, timestamp, file_size, description),
        daemon=True
    )
    upload_thread.start()
    
    # Limpiar progreso después de un tiempo (solo si está completado o con error)
    def cleanup_progress():
        time.sleep(300)  # Esperar 5 minutos antes de limpiar
        if upload_id in upload_progress:
            status = upload_progress[upload_id].get('status', 'unknown')
            # Solo limpiar si está completado o con error
            if status in ['completed', 'error']:
                print(f"🗑️ Limpiando upload_id completado: {upload_id}")
                del upload_progress[upload_id]
            else:
                print(f"⚠️ Upload ID {upload_id} aún en progreso ({status}), no limpiando")
    threading.Thread(target=cleanup_progress, daemon=True).start()
    
    # Devolver upload_id INMEDIATAMENTE
    print(f"📤 Devolviendo upload_id: {upload_id}")
    print(f"📋 Upload IDs disponibles antes de devolver: {list(upload_progress.keys())}")
    
    # Asegurarse de que el upload_id esté en el diccionario antes de devolver
    if upload_id not in upload_progress:
        print(f"⚠️ ADVERTENCIA: upload_id {upload_id} no está en upload_progress, agregándolo...")
        upload_progress[upload_id] = {'progress': 0, 'status': 'uploading', 'current': 0, 'total': file_size}
    
    # Verificar una vez más antes de devolver
    print(f"✅ Verificación final - upload_id en diccionario: {upload_id in upload_progress}")
    
    return jsonify({
        'message': 'Subida iniciada',
        'upload_id': upload_id,
        'status': 'uploading'
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

@app.route('/api/video/<video_id>')
def get_video(video_id):
    """Obtener el video directamente desde la nube de Telegram (sin caché)"""
    # Verificar si hay range request
    range_header = request.headers.get('Range', None)
    
    video_info = get_video_from_db(video_id)
    if not video_info:
        return jsonify({'error': 'Video no encontrado'}), 404
    
    # Obtener phone de la sesión o configuración guardada
    phone = session.get('phone')
    if not phone:
        # NO cargar configuración guardada globalmente
        # Si no hay sesión activa, el usuario debe iniciar sesión
        return jsonify({'error': 'Sesión no disponible. Por favor, inicia sesión.'}), 401
    
    try:
        # Verificar que la sesión es válida para este usuario
        if 'phone' not in session:
            return jsonify({'error': 'Sesión no disponible. Por favor, inicia sesión.'}), 401
        
        # Obtener cliente de Telegram
        try:
            client = get_or_create_client(phone)
            if not client:
                print(f"❌ No se pudo obtener cliente de Telegram para {phone}")
                return jsonify({'error': 'No se pudo conectar a Telegram'}), 500
            
            # Verificar que el cliente esté conectado
            if not client.is_connected():
                print(f"⚠️ Cliente no conectado, intentando conectar...")
                try:
                    run_async(client.connect(), client._loop, timeout=10)
                    if not client.is_connected():
                        print(f"❌ No se pudo conectar el cliente de Telegram")
                        return jsonify({'error': 'No se pudo conectar a Telegram'}), 500
                except Exception as e:
                    print(f"❌ Error conectando cliente: {e}")
                    return jsonify({'error': f'Error de conexión: {str(e)}'}), 500
            
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
        except Exception as e:
            print(f"❌ Error obteniendo cliente: {e}")
            import traceback
            print(traceback.format_exc())
            return jsonify({'error': f'Error obteniendo cliente: {str(e)}'}), 500
        
        # Obtener información del mensaje y el archivo
        async def get_video_info():
            chat_id = video_info.get('chat_id', 'me')
            message_id = video_info['message_id']
            
            target_chat = int(chat_id) if chat_id != 'me' and str(chat_id).isdigit() else 'me'
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
            print(f"❌ Error al obtener información del video {video_id}: {e}")
            import traceback
            print(traceback.format_exc())
            return jsonify({'error': f'Error al obtener el video: {str(e)}'}), 500
        
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
                        
                        # Descargar SOLO el rango solicitado usando GetFileRequest
                        # Esto permite streaming progresivo real - el video empieza a reproducirse inmediatamente
                        try:
                            result = await client(GetFileRequest(
                                location=file_location,
                                offset=start,
                                limit=chunk_size
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
                                # Fallback: descargar completo si GetFileRequest no funciona
                                print(f"⚠️ GetFileRequest no devolvió bytes válidos, usando fallback")
                                buffer = BytesIO()
                                await client.download_media(messages, buffer)
                                buffer.seek(start)
                                return buffer.read(chunk_size)
                        except Exception as e:
                            print(f"⚠️ Error con GetFileRequest: {e}, usando fallback")
                            import traceback
                            print(traceback.format_exc())
                            # Fallback: descargar completo si GetFileRequest falla
                            buffer = BytesIO()
                            await client.download_media(messages, buffer)
                            buffer.seek(start)
                            return buffer.read(chunk_size)
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
            # Usamos 5MB para videos grandes, 3MB para medianos, y 1MB para pequeños
            if file_size > 50 * 1024 * 1024:  # Videos > 50MB
                initial_size = min(5 * 1024 * 1024, file_size)  # 5MB
            elif file_size > 10 * 1024 * 1024:  # Videos > 10MB
                initial_size = min(3 * 1024 * 1024, file_size)  # 3MB
            else:
                initial_size = min(1024 * 1024, file_size)  # 1MB
            
            print(f"📊 Descargando chunk inicial de {initial_size / (1024*1024):.2f}MB para video de {file_size / (1024*1024):.2f}MB")
            
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
                    result = await client(GetFileRequest(
                        location=file_location,
                        offset=0,
                        limit=initial_size
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
                        print(f"✅ GetFileRequest exitoso: {len(data)} bytes descargados")
                        return data
                    else:
                        # Fallback: descargar completo si GetFileRequest no funciona
                        print(f"⚠️ GetFileRequest no devolvió bytes válidos, usando fallback")
                        buffer = BytesIO()
                        await client.download_media(messages, buffer)
                        buffer.seek(0)
                        return buffer.read(initial_size)
                except Exception as e:
                    print(f"⚠️ Error con GetFileRequest en chunk inicial: {e}, usando fallback")
                    import traceback
                    print(traceback.format_exc())
                    # Fallback: descargar completo si GetFileRequest falla
                    buffer = BytesIO()
                    await client.download_media(messages, buffer)
                    buffer.seek(0)
                    return buffer.read(initial_size)
            return None
        
        try:
            initial_data = run_async(download_initial_chunk(), client_loop, timeout=60)
            
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
                return jsonify({'error': 'No se pudo obtener el video'}), 500
        except Exception as e:
            import traceback
            print(f"❌ Error descargando chunk inicial: {e}")
            print(traceback.format_exc())
            return jsonify({'error': str(e)}), 500
            
    except Exception as e:
        import traceback
        print(f"❌ Error obteniendo video: {e}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

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
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)

