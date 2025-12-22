# 🔒 Configurar HTTPS con Let's Encrypt

## Comandos para Configurar HTTPS

Ejecuta estos comandos en tu VPS en orden:

### 1. Instalar Certbot

```bash
apt update
apt install -y certbot python3-certbot-nginx
```

### 2. Obtener Certificado SSL

```bash
certbot --nginx -d makigram.com -d www.makigram.com
```

Durante la instalación, Certbot te preguntará:
- **Email:** Ingresa tu email (para notificaciones de renovación)
- **Términos y condiciones:** Acepta (A)
- **Compartir email:** Opcional, puedes decir No (N)
- **Redirección HTTP a HTTPS:** Selecciona la opción 2 (Redirigir todo el tráfico HTTP a HTTPS)

### 3. Verificar que el Certificado se Instaló

```bash
certbot certificates
```

Deberías ver información sobre tu certificado para `makigram.com` y `www.makigram.com`.

### 4. Probar Renovación Automática

```bash
certbot renew --dry-run
```

Esto verifica que la renovación automática funcionará (no renueva realmente).

### 5. Verificar que HTTPS Funciona

Abre en tu navegador:
- `https://makigram.com`
- `https://www.makigram.com`

Deberías ver el candado verde 🔒 en la barra de direcciones.

## Renovación Automática

Certbot configura automáticamente un cron job o timer de systemd para renovar los certificados. Los certificados de Let's Encrypt duran 90 días y se renuevan automáticamente.

Para verificar el timer de renovación:

```bash
systemctl status certbot.timer
```

## Verificar Configuración de Nginx

Después de ejecutar Certbot, tu configuración de Nginx se actualizará automáticamente. Puedes verla con:

```bash
cat /etc/nginx/sites-available/telegram-app
```

Deberías ver bloques `server` para:
- Puerto 80 (HTTP) - redirige a HTTPS
- Puerto 443 (HTTPS) - sirve la aplicación con SSL

## Actualizar Firewall

Asegúrate de que el puerto 443 (HTTPS) esté abierto:

```bash
ufw allow 443/tcp
ufw status
```

## Solución de Problemas

### Error: "Failed to connect to acme-v02.api.letsencrypt.org"
- Verifica tu conexión a internet
- Verifica que el firewall no esté bloqueando conexiones salientes

### Error: "The domain is not pointing to this server"
- Verifica que los registros DNS estén configurados correctamente
- Espera unos minutos para la propagación de DNS
- Verifica con: `nslookup makigram.com`

### Error: "Too many requests"
- Let's Encrypt tiene límites de rate. Espera 1 hora y vuelve a intentar

### El certificado no se renueva automáticamente
```bash
# Verificar el timer
systemctl status certbot.timer

# Habilitar si no está activo
systemctl enable certbot.timer
systemctl start certbot.timer
```

## Comandos Útiles

```bash
# Ver certificados instalados
certbot certificates

# Renovar certificados manualmente
certbot renew

# Revocar un certificado (si es necesario)
certbot revoke --cert-path /etc/letsencrypt/live/makigram.com/cert.pem

# Ver logs de Certbot
tail -f /var/log/letsencrypt/letsencrypt.log
```

## Notas Importantes

1. **Renovación:** Los certificados se renuevan automáticamente cada 60 días
2. **Backup:** Los certificados se guardan en `/etc/letsencrypt/live/makigram.com/`
3. **Seguridad:** Nunca compartas tus claves privadas
4. **DNS:** Asegúrate de que tu dominio apunte correctamente al servidor antes de obtener el certificado

