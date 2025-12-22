# 🚀 Guía de Despliegue en VPS Hostinger

## Información del VPS
- **IP:** 72.62.83.217
- **Dominio:** makigram.com
- **OS:** Ubuntu 24.04 LTS
- **Acceso:** `ssh root@72.62.83.217`

## Paso 1: Conectarse al VPS

### Opción A: Terminal Web (desde el panel de Hostinger)
1. Ve al panel de Hostinger
2. Haz clic en el botón "Terminal"
3. Se abrirá una terminal web

### Opción B: SSH desde tu PC
```bash
ssh root@72.62.83.217
# Ingresa la contraseña root cuando se solicite
```

## Paso 2: Desplegar la Aplicación

### Método Rápido (Automático)
```bash
# Descargar script de despliegue
curl -o deploy.sh https://raw.githubusercontent.com/Andress232a/telegram/main/deploy.sh
chmod +x deploy.sh
bash deploy.sh
```

### Método Manual
```bash
# 1. Actualizar sistema
apt update && apt upgrade -y

# 2. Instalar dependencias
apt install -y python3 python3-pip python3-venv git nginx

# 3. Clonar repositorio
git clone https://github.com/Andress232a/telegram.git
cd telegram

# 4. Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# 5. Instalar dependencias Python
pip install --upgrade pip
pip install -r requirements.txt

# 6. Crear carpetas necesarias
mkdir -p sessions uploads videos video_cache video_cache_temp
chmod 755 sessions uploads videos video_cache video_cache_temp
```

## Paso 3: Configurar la Aplicación

```bash
# Crear archivo de configuración
nano telegram_config.json
```

Agregar tu configuración:
```json
{
  "api_id": "TU_API_ID",
  "api_hash": "TU_API_HASH"
}
```

## Paso 4: Ejecutar la Aplicación

### Opción A: Ejecución Directa (para pruebas)
```bash
cd telegram
source venv/bin/activate
python app.py
```

### Opción B: Como Servicio Systemd (Recomendado)

1. **Crear servicio:**
```bash
cd telegram
bash deploy-service.sh
```

2. **Iniciar servicio:**
```bash
systemctl start telegram-app
systemctl status telegram-app
```

3. **Ver logs:**
```bash
journalctl -u telegram-app -f
```

## Paso 5: Configurar Nginx (Opcional pero Recomendado)

1. **Copiar configuración:**
```bash
cp nginx-config.conf /etc/nginx/sites-available/telegram-app
nano /etc/nginx/sites-available/telegram-app
# Cambiar "tu-dominio.com" por tu dominio o IP
```

2. **Habilitar sitio:**
```bash
ln -s /etc/nginx/sites-available/telegram-app /etc/nginx/sites-enabled/
nginx -t  # Verificar configuración
systemctl restart nginx
```

3. **Configurar DNS (si aún no está configurado):**
   - En el panel de Hostinger, ve a "Dominios" → "makigram.com"
   - Configura los registros A:
     - `@` → `72.62.83.217`
     - `www` → `72.62.83.217`

4. **Acceder a la aplicación:**
- Con dominio: `http://makigram.com` o `http://www.makigram.com`
- Con IP: `http://72.62.83.217`

## Paso 6: Configurar Firewall (Seguridad)

```bash
# Instalar UFW si no está instalado
apt install -y ufw

# Permitir SSH
ufw allow 22/tcp

# Permitir HTTP
ufw allow 80/tcp

# Permitir HTTPS (si usas SSL)
ufw allow 443/tcp

# IMPORTANTE: NO abrir el puerto 5000 públicamente
# Solo Nginx (puerto 80) debe ser accesible desde fuera
# El puerto 5000 solo debe ser accesible desde localhost

# Activar firewall
ufw enable
ufw status
```

**⚠️ IMPORTANTE:** El puerto 5000 NO debe estar abierto en el firewall. Solo el puerto 80 (Nginx) debe ser accesible desde fuera. La aplicación Flask corre en localhost:5000 y Nginx hace de proxy.

## Comandos Útiles

### Gestión del Servicio
```bash
# Iniciar
systemctl start telegram-app

# Detener
systemctl stop telegram-app

# Reiniciar
systemctl restart telegram-app

# Ver estado
systemctl status telegram-app

# Ver logs en tiempo real
journalctl -u telegram-app -f

# Ver últimos logs
journalctl -u telegram-app -n 100
```

### Actualizar la Aplicación
```bash
cd telegram
git pull
source venv/bin/activate
pip install -r requirements.txt
systemctl restart telegram-app
```

### Verificar que está funcionando
```bash
# Verificar que el puerto 5000 está escuchando
netstat -tlnp | grep 5000

# O con ss
ss -tlnp | grep 5000

# Probar desde el servidor
curl http://localhost:5000
```

## Solución de Problemas

### La aplicación no inicia
```bash
# Ver logs detallados
journalctl -u telegram-app -n 50

# Verificar que el puerto no esté en uso
lsof -i :5000

# Verificar permisos
ls -la telegram/
```

### Error de permisos
```bash
chmod 755 sessions uploads videos video_cache video_cache_temp
chown -R root:root telegram/
```

### La aplicación se detiene
```bash
# Verificar logs del sistema
dmesg | tail

# Verificar recursos
free -h
df -h
```

## Notas Importantes

1. **Seguridad:**
   - Cambia la contraseña root después del primer acceso
   - Configura SSH con claves en lugar de contraseña
   - Considera usar un usuario no-root para la aplicación

2. **Rendimiento:**
   - La aplicación usa memoria para cache de videos
   - Monitorea el uso de disco (videos temporales)
   - Considera usar un proceso manager como supervisor o PM2

3. **Backups:**
   - Configura backups automáticos en Hostinger
   - Haz backup de `telegram_config.json` y `video_database.json`
   - No subas archivos sensibles a GitHub

## Soporte

Si tienes problemas:
1. Revisa los logs: `journalctl -u telegram-app -f`
2. Verifica la configuración de Nginx: `nginx -t`
3. Revisa el firewall: `ufw status`
4. Contacta soporte de Hostinger si es problema del servidor


