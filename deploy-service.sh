#!/bin/bash

# Script para crear servicio systemd
# Ejecutar como root: bash deploy-service.sh

echo "🔧 Configurando servicio systemd..."

# Obtener ruta del proyecto
PROJECT_DIR=$(pwd)
if [ ! -f "$PROJECT_DIR/app.py" ]; then
    echo "❌ Error: No se encontró app.py en el directorio actual"
    echo "   Asegúrate de estar en el directorio del proyecto"
    exit 1
fi

# Crear archivo de servicio
SERVICE_FILE="/etc/systemd/system/telegram-app.service"

cat > $SERVICE_FILE << EOF
[Unit]
Description=Telegram Web App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/venv/bin"
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Recargar systemd y habilitar servicio
systemctl daemon-reload
systemctl enable telegram-app.service

echo "✅ Servicio creado!"
echo ""
echo "📝 Comandos útiles:"
echo "   Iniciar:   systemctl start telegram-app"
echo "   Detener:   systemctl stop telegram-app"
echo "   Estado:    systemctl status telegram-app"
echo "   Logs:      journalctl -u telegram-app -f"






