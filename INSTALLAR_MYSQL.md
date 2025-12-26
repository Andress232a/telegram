# 🗄️ Instalar y Configurar MySQL en el VPS

## Comandos para Ejecutar en el VPS

### 1. Instalar MySQL Server

```bash
apt update
apt install -y mysql-server
```

### 2. Configurar Seguridad de MySQL

```bash
mysql_secure_installation
```

Durante la configuración:
- **Validación de contraseña:** Presiona Enter para usar el nivel medio
- **Nueva contraseña root:** Crea una contraseña segura (guárdala bien)
- **Eliminar usuarios anónimos:** Y (Yes)
- **Deshabilitar login root remoto:** Y (Yes)
- **Eliminar base de datos de prueba:** Y (Yes)
- **Recargar privilegios:** Y (Yes)

### 3. Crear Base de Datos y Usuario

```bash
mysql -u root -p
```

Ingresa la contraseña root que creaste. Luego ejecuta estos comandos SQL:

```sql
-- Crear base de datos
CREATE DATABASE telegram_app CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Crear usuario para la aplicación
CREATE USER 'telegram_user'@'localhost' IDENTIFIED BY 'TU_CONTRASEÑA_SEGURA_AQUI';

-- Dar permisos al usuario
GRANT ALL PRIVILEGES ON telegram_app.* TO 'telegram_user'@'localhost';

-- Aplicar cambios
FLUSH PRIVILEGES;

-- Salir
EXIT;
```

**⚠️ IMPORTANTE:** Reemplaza `TU_CONTRASEÑA_SEGURA_AQUI` con una contraseña fuerte.

### 4. Crear Tablas

```bash
# Desde el directorio del proyecto
cd telegram
mysql -u telegram_user -p telegram_app < database_setup.sql
```

Ingresa la contraseña del usuario `telegram_user`.

### 5. Verificar que las Tablas se Crearon

```bash
mysql -u telegram_user -p telegram_app -e "SHOW TABLES;"
```

Deberías ver:
- `videos`
- `configurations`
- `upload_progress`

### 6. Instalar Dependencias Python para MySQL

```bash
# Activar entorno virtual
source venv/bin/activate

# Instalar PyMySQL o mysql-connector-python
pip install PyMySQL
# O alternativamente:
# pip install mysql-connector-python
```

### 7. Crear Archivo de Configuración de Base de Datos

```bash
nano db_config.json
```

Agrega esto (reemplaza con tus datos reales):

```json
{
  "host": "localhost",
  "user": "telegram_user",
  "password": "TU_CONTRASEÑA_AQUI",
  "database": "telegram_app",
  "charset": "utf8mb4"
}
```

**⚠️ IMPORTANTE:** Agrega `db_config.json` al `.gitignore` para no subirlo a GitHub.

## Verificar Instalación

```bash
# Verificar que MySQL está corriendo
systemctl status mysql

# Probar conexión
mysql -u telegram_user -p telegram_app -e "SELECT COUNT(*) FROM videos;"
```

## Comandos Útiles

```bash
# Iniciar MySQL
systemctl start mysql

# Detener MySQL
systemctl stop mysql

# Reiniciar MySQL
systemctl restart mysql

# Ver logs
tail -f /var/log/mysql/error.log

# Conectarse a MySQL
mysql -u telegram_user -p telegram_app

# Backup de la base de datos
mysqldump -u telegram_user -p telegram_app > backup_$(date +%Y%m%d).sql

# Restaurar backup
mysql -u telegram_user -p telegram_app < backup_20241222.sql
```

## Solución de Problemas

### Error: "Access denied for user"
- Verifica que el usuario y contraseña sean correctos
- Verifica que el usuario tenga permisos: `SHOW GRANTS FOR 'telegram_user'@'localhost';`

### Error: "Can't connect to MySQL server"
- Verifica que MySQL esté corriendo: `systemctl status mysql`
- Inicia MySQL: `systemctl start mysql`

### Error: "Table doesn't exist"
- Verifica que ejecutaste `database_setup.sql`
- Verifica que estás usando la base de datos correcta: `USE telegram_app;`

## Seguridad

1. **Nunca subas `db_config.json` a GitHub** - ya está en `.gitignore`
2. **Usa contraseñas fuertes** para el usuario de MySQL
3. **Solo permite conexiones desde localhost** (ya está configurado así)
4. **Haz backups regulares** de la base de datos



