# Despliegue en PythonAnywhere

## Preparar el proyecto

Sube el contenido de esta carpeta y conserva `static/db/tennissel2.0.sql`, `static/IMG` y `static/uploads`. En una consola Bash de PythonAnywhere:

```bash
cd ~/TENNISSELes
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

En **Web > WSGI configuration file**, importa la aplicación con:

```python
import os
import sys

project_root = '/home/tu_usuario/TENNISSELes'
if project_root not in sys.path:
    sys.path.insert(0, project_root)

os.environ['DATABASE_PATH'] = os.path.join(
    project_root, 'instance', 'tennissel.sqlite3'
)
os.environ['SECRET_KEY'] = 'reemplaza-por-un-secreto-largo-y-aleatorio'

from app import app as application
```

Genera un `SECRET_KEY` privado y estable; no lo publiques ni lo cambies en cada recarga. Selecciona el virtualenv en la configuración de la aplicación web y mapea `/static/` a la carpeta `static` del proyecto. Recarga la aplicación después de guardar la configuración.

La base SQLite se crea en `instance/tennissel.sqlite3` en el primer acceso. El directorio se crea automáticamente y debe permanecer escribible por la aplicación. El esquema y los datos iniciales de productos, ubicaciones y contenido se toman de `static/db/tennissel2.0.sql`; la inicialización no ejecuta las instrucciones `DROP TABLE` del volcado. Si ya existe una base con datos, no la reemplaces: conserva primero una copia.

## Crear el administrador

No hay una contraseña administrativa fija o publicada. Después de instalar las dependencias, crea una cuenta con contraseña privada desde la consola:

```bash
cd ~/TENNISSELes
source .venv/bin/activate
flask --app app create-admin --username admin --email admin@tu-dominio.com
```

El comando solicita la contraseña sin mostrarla y exige al menos 12 caracteres. También puedes definir `ADMIN_USERNAME`, `ADMIN_EMAIL` y `ADMIN_PASSWORD` en la configuración WSGI antes del primer acceso; si no existe la cuenta, se aprovisiona automáticamente. La inicialización no cambia la contraseña de una cuenta existente.

## Imágenes, mapas, reservas y pagos

- Los archivos de catálogo y ubicaciones se sirven desde `static/IMG` y `static/uploads`. Conserva las mayúsculas/minúsculas de los nombres al subir a Linux; el catálogo resuelve nombres de archivo sin distinguir mayúsculas para tolerar imágenes creadas originalmente en Windows.
- Folium, los mosaicos del mapa y la geocodificación de direcciones requieren acceso a Internet desde el navegador/servidor. También se puede centrar el mapa ingresando coordenadas `latitud, longitud`.
- SQLite usa un archivo local persistente. No lo guardes en un directorio temporal ni lo compartas entre réplicas/servidores; PythonAnywhere debe ejecutar la aplicación con permisos de lectura y escritura sobre `instance/`.
- El checkout y Premium existentes son explícitamente de **demostración**; no se realiza un cobro real. Para aceptar pagos reales hace falta contratar e integrar una pasarela, configurar sus credenciales/webhooks y probar su entorno de producción por separado.

Las sesiones usan las cookies firmadas de Flask, por lo que todas las instancias deben compartir el mismo `SECRET_KEY`.
