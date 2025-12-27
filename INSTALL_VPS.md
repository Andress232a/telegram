# 📋 Instrucciones Rápidas para VPS

## Ya clonaste el repositorio ✅

Ahora sigue estos pasos:

### 1. Instalar pip
```bash
apt update
apt install -y python3-pip python3-venv
```

### 2. Crear entorno virtual
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Instalar dependencias
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Crear carpetas necesarias
```bash
mkdir -p sessions uploads videos video_cache video_cache_temp
chmod 755 sessions uploads videos video_cache video_cache_temp
```

### 5. Configurar credenciales
```bash
nano telegram_config.json
```

Pega tu configuración (ya la tienes en tu PC):
```json
{
  "api_id": "31988198",
  "api_hash": "8059bc6b49c191c3ad874ded21db9aee",
  "phone": "+573118257410",
  "session_name": "sessions/573118257410"
}
```

### 6. Ejecutar la aplicación

**Opción A: Prueba rápida**
```bash
source venv/bin/activate
python app.py
```

**Opción B: Como servicio (recomendado)**

1. Crear servicio systemd:
```bash
cat > /etc/systemd/system/telegram-app.service << 'EOF'
[Unit]
Description=Telegram Web App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/telegram
Environment="PATH=/root/telegram/venv/bin"
ExecStart=/root/telegram/venv/bin/python /root/telegram/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

2. Activar e iniciar:
```bash
systemctl daemon-reload
systemctl enable telegram-app
systemctl start telegram-app
systemctl status telegram-app
```

3. Ver logs:
```bash
journalctl -u telegram-app -f
```

### 7. Acceder a la aplicación

La aplicación estará disponible en:
- **Desde el servidor:** `http://localhost:5000`
- **Desde fuera:** `http://72.62.83.217:5000`

### 8. (Opcional) Configurar Nginx como proxy

Si quieres usar el puerto 80 estándar:

```bash
apt install -y nginx

cat > /etc/nginx/sites-available/telegram-app << 'EOF'
server {
    listen 80;
    server_name 72.62.83.217;

    client_max_body_size 2G;
    client_body_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
EOF

ln -s /etc/nginx/sites-available/telegram-app /etc/nginx/sites-enabled/
nginx -t
systemctl restart nginx
```

Luego accede en: `http://72.62.83.217`

## Comandos Útiles

```bash
# Ver estado del servicio
systemctl status telegram-app

# Reiniciar servicio
systemctl restart telegram-app

# Ver logs en tiempo real
journalctl -u telegram-app -f

# Detener servicio
systemctl stop telegram-app

# Verificar que está escuchando
netstat -tlnp | grep 5000
```






