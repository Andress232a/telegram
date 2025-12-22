#!/bin/bash

# Script de despliegue para VPS Hostinger
# Ejecutar en el servidor: bash deploy.sh

echo "🚀 Iniciando despliegue de la aplicación Telegram..."

# Actualizar sistema
echo "📦 Actualizando sistema..."
apt update && apt upgrade -y

# Instalar dependencias del sistema
echo "📦 Instalando dependencias del sistema..."
apt install -y python3 python3-pip python3-venv git nginx

# Clonar repositorio (si no existe)
if [ ! -d "telegram" ]; then
    echo "📥 Clonando repositorio de GitHub..."
    git clone https://github.com/Andress232a/telegram.git
fi

cd telegram

# Crear entorno virtual
if [ ! -d "venv" ]; then
    echo "🔧 Creando entorno virtual..."
    python3 -m venv venv
fi

# Activar entorno virtual e instalar dependencias
echo "📦 Instalando dependencias de Python..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Crear carpetas necesarias
echo "📁 Creando carpetas necesarias..."
mkdir -p sessions uploads videos video_cache video_cache_temp

# Configurar permisos
echo "🔐 Configurando permisos..."
chmod 755 sessions uploads videos video_cache video_cache_temp

# Configurar Nginx
echo "🌐 Configurando Nginx..."
if [ -f "nginx-config.conf" ]; then
    cp nginx-config.conf /etc/nginx/sites-available/telegram-app
    ln -sf /etc/nginx/sites-available/telegram-app /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl restart nginx
    echo "✅ Nginx configurado"
else
    echo "⚠️  nginx-config.conf no encontrado, configura Nginx manualmente"
fi

# Configurar firewall básico
echo "🔥 Configurando firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    echo "✅ Reglas de firewall configuradas (no activadas automáticamente)"
    echo "   Ejecuta 'ufw enable' para activar el firewall"
else
    echo "⚠️  UFW no está instalado, instala con: apt install -y ufw"
fi

echo "✅ Despliegue completado!"
echo ""
echo "📝 Próximos pasos:"
echo "1. Configura telegram_config.json con tus credenciales"
echo "2. Configura el servicio systemd: bash deploy-service.sh"
echo "3. Inicia el servicio: systemctl start telegram-app"
echo "4. Verifica logs: journalctl -u telegram-app -f"
echo "5. Accede a: http://makigram.com"
echo ""
echo "📖 Para más detalles, ver: CONFIGURAR_DOMINIO.md"


