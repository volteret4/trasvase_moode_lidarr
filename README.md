# Trasvase

Parte de [REPEAT-ONE](https://github.com/volteret4/repeat-one-web). Compara dos colecciones de
música (por ejemplo, la principal y la carpeta de descargas) y copia a la principal los discos
que le faltan, con filtros por artista, género o sello.

[Demo con datos reales](https://volteret4.github.io/repeat-one-web/demo/trasvase/)

- **`/`**: discos de la colección 2 que faltan en la 1; seleccionar y copiar a una carpeta temporal.
- **`/temp`**: lo que hay en la carpeta temporal, con duplicados y aviso a Airsonic/Navidrome.

## Instalación

Con el instalador de REPEAT-ONE (módulo `trasvase`). Las dos colecciones y la carpeta temporal
se montan como volúmenes en `/mnt/music/coleccion1`, `/mnt/music/coleccion2` y `/mnt/music/temp`
(ver `config.py`). El resto de opciones, en `.env` (ver `.env.example`).

```bash
python scan.py
```

```bash
python app.py
```

## Instalación con Docker Compose

```bash
cp .env.example .env
```

Rellena `.env` (credenciales y carpetas de tu equipo) y arranca:

```bash
docker compose up -d --build
```

Los datos que genera (bases, cachés) se quedan en esta misma carpeta, fuera de git.

## Actualizaciones automáticas

El `docker-compose.yml` trae su propia [Ofelia](https://github.com/mcuadros/ofelia), que lanza dentro del contenedor los mismos trabajos y a las mismas horas que en REPEAT-ONE (formato con segundos: `seg min hora día mes día-semana`, hora de `TZ`, por defecto Europe/Madrid):

| Trabajo | Cuándo | Qué lanza |
|---|---|---|
| `trasvase-scan` | `0 50 0 * * *` | `python3 scan.py` |

Para cambiar una hora, edita su etiqueta `ofelia.job-exec.<trabajo>.schedule` en `docker-compose.yml` y vuelve a hacer `docker compose up -d`. Para lanzarlo a mano: `docker compose exec <servicio> <orden>`. Para no tener actualizaciones automáticas: `docker compose stop ofelia`.

Esta Ofelia solo ve los contenedores de este repo (etiqueta `repeat-one.ofelia`), así que puedes tener varios módulos instalados por separado sin que se pisen. Necesita el socket de Docker en solo lectura.
