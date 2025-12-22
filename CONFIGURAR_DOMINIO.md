# 🌐 Configuración del Dominio makigram.com

## Problema: ERR_CONNECTION_TIMED_OUT

Si ves este error, significa que:
1. El puerto 5000 no está accesible desde fuera (es correcto, debe estar así)
2. Nginx no está configurado o no está corriendo
3. El firewall está bloqueando el puerto 80

## Solución Paso a Paso

### 1. Verificar que la aplicación está corriendo

```bash
# Verificar que el servicio está activo
systemctl status telegram-app

# Si no está corriendo, iniciarlo
systemctl start telegram-app

# Ver logs
journalctl -u telegram-app -f
```

### 2. Verificar que la aplicación escucha en localhost:5000

```bash
# Verificar que el puerto 5000 está escuchando
netstat -tlnp | grep 5000
# Debe mostrar: tcp 0 0 0.0.0.0:5000 0.0.0.0:* LISTEN

# Probar desde el servidor
curl http://localhost:5000
```

### 3. Instalar y configurar Nginx

```bash
# Instalar Nginx si no está instalado
apt install -y nginx

# Copiar configuración
cp nginx-config.conf /etc/nginx/sites-available/telegram-app

# Habilitar sitio
ln -s /etc/nginx/sites-available/telegram-app /etc/nginx/sites-enabled/

# Eliminar configuración por defecto (opcional)
rm -f /etc/nginx/sites-enabled/default

# Verificar configuración
nginx -t

# Reiniciar Nginx
systemctl restart nginx

# Verificar que Nginx está corriendo
systemctl status nginx
```

### 4. Configurar Firewall

```bash
# Instalar UFW si no está instalado
apt install -y ufw

# Verificar estado actual
ufw status

# Permitir puertos necesarios
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP
ufw allow 443/tcp   # HTTPS (para futuro SSL)

# IMPORTANTE: NO abrir el puerto 5000
# Solo debe ser accesible desde localhost

# Activar firewall
ufw enable

# Verificar reglas
ufw status numbered
```

### 5. Configurar DNS en Hostinger

1. Ve al panel de Hostinger
2. Navega a "Dominios" → "makigram.com"
3. Ve a "Zona DNS" o "DNS Records"
4. Configura los registros A:
   - **Tipo:** A
   - **Nombre:** @ (o vacío)
   - **Valor:** 72.62.83.217
   - **TTL:** 3600 (o automático)
   
   - **Tipo:** A
   - **Nombre:** www
   - **Valor:** 72.62.83.217
   - **TTL:** 3600 (o automático)

5. Espera 5-10 minutos para que los cambios de DNS se propaguen

### 6. Verificar que todo funciona

```bash
# Desde el servidor, probar Nginx
curl http://localhost

# Verificar que Nginx está escuchando en el puerto 80
netstat -tlnp | grep 80
# Debe mostrar: tcp 0 0 0.0.0.0:80 0.0.0.0:* LISTEN nginx
```

### 7. Probar desde tu navegador

- Abre: `http://makigram.com`
- O: `http://www.makigram.com`

Si aún no funciona, espera unos minutos más para la propagación de DNS.

## Verificación Completa

Ejecuta estos comandos para verificar todo:

```bash
# 1. Aplicación corriendo
systemctl status telegram-app | grep Active

# 2. Nginx corriendo
systemctl status nginx | grep Active

# 3. Puerto 5000 escuchando (solo localhost)
netstat -tlnp | grep 5000

# 4. Puerto 80 escuchando (público)
netstat -tlnp | grep :80

# 5. Firewall permitiendo puerto 80
ufw status | grep 80

# 6. Probar localmente
curl -I http://localhost:5000
curl -I http://localhost
```

## Solución de Problemas

### Error: "502 Bad Gateway"
- La aplicación Flask no está corriendo
- Solución: `systemctl start telegram-app`

### Error: "Connection refused"
- Nginx no está corriendo
- Solución: `systemctl start nginx`

### Error: "ERR_CONNECTION_TIMED_OUT"
- El firewall está bloqueando el puerto 80
- Solución: `ufw allow 80/tcp && ufw reload`

### El dominio no resuelve
- DNS no está configurado o no se ha propagado
- Verifica en: https://www.whatsmydns.net/#A/makigram.com
- Espera hasta 24 horas para propagación completa

### Nginx error: "address already in use"
- Otro servicio está usando el puerto 80
- Solución: `lsof -i :80` para ver qué proceso lo usa

## Configuración SSL (Opcional - Futuro)

Para habilitar HTTPS con Let's Encrypt:

```bash
# Instalar Certbot
apt install -y certbot python3-certbot-nginx

# Obtener certificado
certbot --nginx -d makigram.com -d www.makigram.com

# Renovar automáticamente
certbot renew --dry-run
```

Luego actualiza `nginx-config.conf` para incluir la configuración SSL.

