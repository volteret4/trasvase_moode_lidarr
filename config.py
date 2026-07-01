import os
from sops_env import load_sops_env

try:
    load_sops_env()
except FileNotFoundError:
    # En Docker las variables llegan ya descifradas vía docker-compose (scripts/up.sh)
    print("Aviso: no se encontró .encrypted.env; usando variables de entorno ya exportadas")

COLLECTION_1_PATH = os.environ.get("COLLECTION_1_PATH", "/mnt/music/coleccion1")
COLLECTION_2_PATH = os.environ.get("COLLECTION_2_PATH", "/mnt/music/coleccion2")
TEMP_PATH         = os.environ.get("TEMP_PATH", os.path.join(
                        os.environ.get("COLLECTION_1_PATH", "/mnt/music/coleccion1"), "temp", "auto-dietpi"
                    ))
DB_PATH           = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music.db")

FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))

# Airsonic / Subsonic API
AIRSONIC_URL      = os.environ.get("AIRSONIC_URL",      "http://localhost:4040")
AIRSONIC_USER     = os.environ.get("AIRSONIC_USER",     "admin")
AIRSONIC_PASSWORD = os.environ.get("AIRSONIC_PASSWORD", "")

# Last.fm (optional — for cover art fallback)
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
