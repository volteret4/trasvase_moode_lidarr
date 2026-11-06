import os
from sops_env import load_sops_env

try:
    load_sops_env()
except FileNotFoundError:
    # En Docker las variables llegan ya descifradas vía docker-compose (scripts/up.sh)
    print("Aviso: no se encontró .encrypted.env; usando variables de entorno ya exportadas")

# Rutas FIJAS dentro del contenedor -- no configurables por variable de
# entorno a propósito. La ruta real del host se elige con
# TRASVASE_COLLECTION_1_PATH/_2_PATH (ver docker-compose.yml), que docker
# monta siempre en estos mismos puntos internos; un COLLECTION_1_PATH
# suelto en el .env (fácil de confundir con el TRASVASE_-prefijado) apuntaría
# a una ruta del host que no existe dentro del contenedor y el escaneo
# encontraría 0 álbumes en silencio.
COLLECTION_1_PATH = "/mnt/music/coleccion1"
COLLECTION_2_PATH = "/mnt/music/coleccion2"
# temp está fuera de la colección 1 (host: /ruta/al/nas/moode/temp, montado
# por TRASVASE_TEMP_PATH); scan.py lo cuenta como colección 1
TEMP_ROOT         = "/mnt/music/temp"
TEMP_PATH         = os.path.join(TEMP_ROOT, "auto-dietpi")
DB_PATH           = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music.db")

FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))

# Airsonic / Subsonic API
AIRSONIC_URL      = os.environ.get("AIRSONIC_URL",      "http://localhost:4040")
AIRSONIC_USER     = os.environ.get("AIRSONIC_USER",     "admin")
AIRSONIC_PASSWORD = os.environ.get("AIRSONIC_PASSWORD", "")

# Last.fm (optional — for cover art fallback)
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
