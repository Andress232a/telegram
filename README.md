# 📱 Gestor de Telegram

Una aplicación web para gestionar y compartir videos de Telegram con URLs alternativas que no requieren tener Telegram instalado.

## 🚀 Características

- ✅ Configuración fácil de la API de Telegram
- ✅ Autenticación con código de verificación
- ✅ Soporte para autenticación de dos factores (2FA)
- ✅ Subida de videos a Telegram
- ✅ Generación de URLs alternativas para ver videos sin Telegram
- ✅ Interfaz web moderna y fácil de usar

## 📋 Requisitos

- Python 3.7 o superior
- Cuenta de Telegram
- API ID y API Hash de Telegram (obtener en [my.telegram.org/apps](https://my.telegram.org/apps))

## 🔧 Instalación

1. **Clonar o descargar el proyecto**

2. **Instalar dependencias:**
```bash
pip install -r requirements.txt
```

## 🎯 Uso

1. **Iniciar la aplicación:**
```bash
python app.py
```

2. **Abrir en el navegador:**
   - Ve a `http://localhost:5000`

3. **Configurar tu cuenta de Telegram:**
   - Ingresa tu **API ID** (obtener en [my.telegram.org/apps](https://my.telegram.org/apps))
   - Ingresa tu **API Hash** (obtener en [my.telegram.org/apps](https://my.telegram.org/apps))
   - Ingresa tu **número de teléfono** con código de país (ej: +34612345678)

4. **Conectar:**
   - La aplicación enviará un código de verificación a Telegram
   - Ingresa el código recibido
   - Si tienes 2FA activado, ingresa tu contraseña

5. **Subir videos:**
   - Una vez conectado, podrás subir videos arrastrándolos o seleccionándolos
   - Los videos se subirán a tus "Mensajes Guardados" en Telegram
   - Obtendrás una URL alternativa para compartir el video

6. **Ver tus videos:**
   - Ve a "Mis Videos" para ver todos los videos subidos
   - Haz clic en cualquier video para verlo con la URL alternativa

## 📁 Estructura del Proyecto

```
telegram/
├── app.py                 # Aplicación Flask principal
├── requirements.txt       # Dependencias de Python
├── README.md             # Este archivo
├── templates/            # Plantillas HTML
│   ├── index.html        # Página de configuración
│   ├── upload.html       # Página de subida
│   ├── watch.html        # Página de visualización
│   └── list.html         # Lista de videos
├── sessions/             # Sesiones de Telegram (se crea automáticamente)
├── uploads/             # Archivos temporales (se crea automáticamente)
└── videos/              # Videos descargados (se crea automáticamente)
```

## 🔐 Seguridad

- Las sesiones de Telegram se guardan localmente en la carpeta `sessions/`
- Los videos se almacenan temporalmente durante la subida
- Las URLs generadas son únicas y seguras
- La aplicación solo accede a tus "Mensajes Guardados"

## ⚠️ Notas Importantes

- Los videos se suben a tus "Mensajes Guardados" en Telegram
- Las URLs alternativas funcionan mientras la aplicación esté ejecutándose
- Los videos se descargan desde Telegram cuando se solicitan
- El tamaño máximo de video es 2GB

## 🛠️ Solución de Problemas

### Error: "No se puede leer la secuencia"
- Esto es un error de PowerShell al crear archivos, pero los archivos se crearon correctamente
- Puedes ignorar este error

### Error al conectar a Telegram
- Verifica que tu API ID y API Hash sean correctos
- Asegúrate de que tu número de teléfono incluya el código de país con el signo +
- Verifica tu conexión a internet

### Error al subir video
- Verifica que estés conectado a Telegram
- Asegúrate de que el video no exceda 2GB
- Verifica que el formato del video sea compatible

## 📝 Licencia

Este proyecto es de código abierto y está disponible para uso personal.

## 🤝 Contribuciones

Las contribuciones son bienvenidas. Siéntete libre de abrir un issue o pull request.






