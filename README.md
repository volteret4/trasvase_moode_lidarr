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
