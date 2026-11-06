#!/usr/bin/env python3
"""
Flask app — music collection manager.
Serves two web interfaces:
  /          → trasvase: albums in C2 missing from C1
  /temp      → temp manager: albums in C1/temp/, with duplicate detection + Airsonic integration
"""

import hashlib
import os
import secrets
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import date

import requests
from flask import Flask, jsonify, request, render_template

from config import (
    AIRSONIC_PASSWORD, AIRSONIC_URL, AIRSONIC_USER,
    COLLECTION_1_PATH, COLLECTION_2_PATH, DB_PATH,
    FLASK_HOST, FLASK_PORT, LASTFM_API_KEY, TEMP_PATH,
)

app = Flask(__name__)

# ── Panel de configuración (⚙) ───────────────────────────────────────────────
# Mismo patrón que dieta_sonora: AIRSONIC_*/LASTFM_API_KEY van al .env propio
# (montado en /app/.env). Las 2 rutas de colección son distintas: el
# contenedor solo conoce los mount points fijos (/mnt/music/coleccion1/2);
# la ruta REAL del host la fija TRASVASE_COLLECTION_1_PATH/_2_PATH en el .env
# raíz de tumtumpa, que es de donde docker-compose interpola los volumes —
# cambiarlas requiere RECREAR el contenedor, no solo reiniciarlo.
SETTINGS_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
SETTINGS_ROOT_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "root.env")
SETTINGS_PASSWORD = os.environ.get("SETTINGS_PASSWORD", "")
VARS_SPEC = [
    {"name": "AIRSONIC_URL", "secret": False, "help": "URL base de Airsonic"},
    {"name": "AIRSONIC_USER", "secret": False, "help": "Usuario Airsonic"},
    {"name": "AIRSONIC_PASSWORD", "secret": True, "help": "Contraseña Airsonic"},
    {"name": "LASTFM_API_KEY", "secret": True, "help": "API key de Last.fm"},
    {"name": "TRASVASE_COLLECTION_1_PATH", "secret": False, "location": "root",
     "help": "Colección 1 = raíz de 'moode' (la colección completa que sirve Airsonic; dentro tiene temp/ para discos sin decidir). Ruta host (bind mount — requiere recrear el contenedor)"},
    {"name": "TRASVASE_COLLECTION_2_PATH", "secret": False, "location": "root",
     "help": "Colección 2 = ruta de 'lidarr' (descargas), carpeta/disco totalmente distinto de la Colección 1. Ruta host (bind mount — requiere recrear el contenedor)"},
]
_HAS_SECRETS = any(v.get("secret") for v in VARS_SPEC)


def _settings_path(spec):
    return SETTINGS_ROOT_ENV_PATH if spec.get("location") == "root" else SETTINGS_ENV_PATH


def _read_env_file(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            values[k.strip()] = v
    return values


def _write_env_file(path, updates):
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}\n")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"{k}={v}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)


def _current_value(spec):
    path = _settings_path(spec)
    file_vals = _read_env_file(path)
    if spec["name"] in file_vals:
        return file_vals[spec["name"]]
    return os.environ.get(spec["name"], spec.get("default", ""))


def _check_auth(password):
    if not SETTINGS_PASSWORD:
        return not _HAS_SECRETS
    return password == SETTINGS_PASSWORD


@app.route("/api/settings", methods=["POST"])
def api_settings():
    d = request.get_json(silent=True) or {}
    password = d.get("password") or ""
    requires = bool(SETTINGS_PASSWORD) or _HAS_SECRETS
    authorized = _check_auth(password)
    if requires and not authorized:
        error = "Contraseña incorrecta" if password else None
        if not SETTINGS_PASSWORD:
            error = "Este servicio tiene credenciales pero no hay SETTINGS_PASSWORD configurada. Añádela al .env y reinicia el contenedor."
        return jsonify({"requires_password": True, "authorized": False, "error": error})
    vars_out = [
        {"name": v["name"], "value": _current_value(v), "secret": v["secret"], "help": v.get("help", "")}
        for v in VARS_SPEC
    ]
    return jsonify({"requires_password": requires, "authorized": True, "vars": vars_out})


@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    d = request.get_json(silent=True) or {}
    if not _check_auth(d.get("password") or ""):
        return jsonify({"error": "Contraseña incorrecta"}), 403
    by_name = {v["name"]: v for v in VARS_SPEC}
    updates = {k: v for k, v in (d.get("values") or {}).items() if k in by_name}
    if not updates:
        return jsonify({"error": "Nada que guardar"}), 400
    by_path = {}
    for k, v in updates.items():
        path = _settings_path(by_name[k])
        by_path.setdefault(path, {})[k] = v
    for path, vals in by_path.items():
        _write_env_file(path, vals)
    touched_root = any(_settings_path(by_name[k]) == SETTINGS_ROOT_ENV_PATH for k in updates)
    message = "Guardado. Reinicia el contenedor para aplicar los cambios."
    if touched_root:
        message = ("Guardado. Alguna variable es una ruta de bind mount — hace falta "
                    "RECREAR el contenedor (docker compose up -d --build), no basta con reiniciarlo.")
    return jsonify({"ok": True, "message": message})


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_db(conn):
    """Add columns introduced after the initial schema."""
    for col, defn in [
        ("file_count",  "INTEGER DEFAULT 0"),
        ("audio_count", "INTEGER DEFAULT 0"),
        ("cover_path",  "TEXT"),
        ("missing",     "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE albums ADD COLUMN {col} {defn}")
            conn.commit()
        except Exception:
            pass


# Run migration once at startup
with get_db() as _mconn:
    _migrate_db(_mconn)


MISSING_QUERY = """
    SELECT a2.id, ar.name AS artist, a2.title, a2.label, a2.genre, a2.year,
           a2.path, a2.file_count, a2.audio_count, a2.cover_path
    FROM albums a2
    JOIN artists ar ON a2.artist_id = ar.id
    WHERE a2.collection = 2 AND a2.missing = 1
    ORDER BY ar.name COLLATE NOCASE, a2.year DESC, a2.title COLLATE NOCASE
"""
# Nota: "missing" lo calcula scan.py (recompute_missing) una vez por escaneo,
# no en cada carga -- antes esta query comparaba LOWER(artista)+LOWER(título)
# contra toda la colección 1 con NOT EXISTS (sin índice posible), ~6s por
# ejecución, y /api/stats la repetía una segunda vez. Si algún día hace falta
# recalcularlo sin pasar por scan.py completo, llamar a scan.recompute_missing.


# ---------------------------------------------------------------------------
# Airsonic / Subsonic helpers
# ---------------------------------------------------------------------------

def _airsonic_params(**extra):
    """Build Subsonic API auth params using token-based auth."""
    salt  = secrets.token_hex(8)
    token = hashlib.md5((AIRSONIC_PASSWORD + salt).encode()).hexdigest()
    return {"u": AIRSONIC_USER, "t": token, "s": salt,
            "v": "1.16.1", "c": "trasvase", "f": "json", **extra}


def airsonic(endpoint, **kwargs):
    """Call Subsonic API, return the response data dict or raise."""
    url  = f"{AIRSONIC_URL}/rest/{endpoint}.view"
    resp = requests.get(url, params=_airsonic_params(**kwargs), timeout=10)
    resp.raise_for_status()
    data = resp.json().get("subsonic-response", {})
    if data.get("status") != "ok":
        err = data.get("error", {})
        raise RuntimeError(err.get("message", "Unknown Airsonic error"))
    return data


def airsonic_coverart_url(cover_id):
    """Return a proxy URL for a cover art image."""
    return f"/api/airsonic/coverart?id={cover_id}"


# ---------------------------------------------------------------------------
# Frontend routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/temp")
def temp_page():
    return render_template("temp.html")


# ---------------------------------------------------------------------------
# API — trasvase (C2 → C1)
# ---------------------------------------------------------------------------

# A folder creation time shared by this many albums comes from a bulk copy, not
# from when each album arrived.
BULK_COPY_MIN_ALBUMS = 50


def _birth_times(paths):
    """Return {path: epoch} with each folder's creation time, read from disk.

    Python 3.12's os.stat doesn't expose it on Linux, hence stat(1). Paths it
    can't date (missing, or a filesystem without birth time) are left out, and
    stat's non-zero exit for a missing path still leaves the others usable.
    """
    if not paths:
        return {}
    try:
        res = subprocess.run(
            ["stat", "--printf", "%W|%n\\0", "--", *paths],
            capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    births = {}
    for entry in res.stdout.split("\0"):
        birth, _, path = entry.partition("|")
        if birth.isdigit() and int(birth) > 0:
            births[path] = int(birth)
    return births


def _added_times(paths):
    """Like _birth_times, but bulk-copied albums use their older mtime instead.

    An mtime newer than the bulk copy means the album changed afterwards, so
    the copy's date stays the better estimate.
    """
    births = _birth_times(paths)
    per_birth = Counter(births.values())
    added = {}
    for path, birth in births.items():
        added[path] = birth
        if per_birth[birth] >= BULK_COPY_MIN_ALBUMS:
            try:
                added[path] = min(birth, int(os.stat(path).st_mtime))
            except OSError:
                pass
    return added


def _give_to_temp_owner(path):
    """Chown a tree to TEMP_PATH's owner.

    This container runs as root, and dieta-sonora (uid 1000) copies into the
    same per-day folders: if one stays root's, its copies fail for that day.
    """
    if os.geteuid() != 0:
        return
    owner = os.stat(TEMP_PATH)
    for root, dirs, files in os.walk(path):
        os.chown(root, owner.st_uid, owner.st_gid)
        for name in dirs + files:
            os.chown(os.path.join(root, name), owner.st_uid, owner.st_gid, follow_symlinks=False)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Reescanea las dos colecciones (scan.py) y sincroniza la base de datos."""
    scan_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.py")
    try:
        res = subprocess.run(
            ["python3", scan_script],
            capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "El escaneo tardó demasiado (timeout)"}), 500
    if res.returncode != 0:
        return jsonify({"error": res.stderr or "Error ejecutando scan.py"}), 500
    return jsonify({"success": True, "message": "Colecciones reescaneadas", "log": res.stdout[-2000:]})


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    stats = {
        "collection1": conn.execute("SELECT COUNT(*) FROM albums WHERE collection=1").fetchone()[0],
        "collection2": conn.execute("SELECT COUNT(*) FROM albums WHERE collection=2").fetchone()[0],
        "artists":     conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0],
        "missing":     conn.execute("SELECT COUNT(*) FROM albums WHERE collection=2 AND missing=1").fetchone()[0],
    }
    conn.close()
    return jsonify(stats)


@app.route("/api/albums/missing")
def api_missing():
    conn = get_db()
    rows = conn.execute(MISSING_QUERY).fetchall()
    conn.close()
    added = _added_times([r["path"] for r in rows])
    return jsonify([dict(r) | {"added_at": added.get(r["path"])} for r in rows])


@app.route("/api/albums/copy", methods=["POST"])
def api_copy():
    data       = request.get_json(silent=True) or {}
    album_ids  = data.get("ids", [])
    if not album_ids:
        return jsonify({"error": "No album IDs provided"}), 400

    conn    = get_db()
    results = []

    for album_id in album_ids:
        row = conn.execute("""
            SELECT a.id, a.path, a.title, a.artist_id, ar.name AS artist,
                   a.label, a.genre, a.year
            FROM albums a
            JOIN artists ar ON a.artist_id = ar.id
            WHERE a.id = ? AND a.collection = 2
        """, (album_id,)).fetchone()

        if not row:
            results.append({"id": album_id, "success": False, "error": "Album not found"})
            continue

        src_path    = row["path"]
        album_folder = os.path.basename(src_path)
        date_str    = date.today().strftime("%Y-%m-%d")
        dst_path    = os.path.join(TEMP_PATH, date_str, album_folder)

        if os.path.exists(dst_path):
            results.append({"id": album_id, "success": False, "error": "Destination already exists"})
            continue

        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            _give_to_temp_owner(os.path.dirname(dst_path))
            try:
                shutil.copytree(src_path, dst_path)
            finally:
                _give_to_temp_owner(dst_path)

            conn.execute("INSERT INTO artists(name) VALUES(?) ON CONFLICT(name) DO NOTHING",
                         (row["artist"],))
            artist_id = conn.execute(
                "SELECT id FROM artists WHERE name=? COLLATE NOCASE", (row["artist"],)
            ).fetchone()[0]
            conn.execute("""
                INSERT INTO albums(artist_id, title, label, genre, year, path, collection)
                VALUES(?, ?, ?, ?, ?, ?, 1) ON CONFLICT(path) DO NOTHING
            """, (artist_id, row["title"], row["label"], row["genre"], row["year"], dst_path))
            conn.commit()
            results.append({"id": album_id, "success": True, "dest": dst_path})

        except Exception as exc:
            results.append({"id": album_id, "success": False, "error": str(exc)})

    conn.close()
    return jsonify(results)


@app.route("/api/albums/<int:album_id>", methods=["DELETE"])
def api_delete_album(album_id):
    conn = get_db()
    row  = conn.execute(
        "SELECT id, path, artist_id FROM albums WHERE id=?", (album_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Album not found"}), 404
    try:
        if os.path.exists(row["path"]):
            shutil.rmtree(row["path"])
        conn.execute("DELETE FROM albums WHERE id=?", (album_id,))
        conn.execute("""
            DELETE FROM artists WHERE id=?
              AND id NOT IN (SELECT DISTINCT artist_id FROM albums)
        """, (row["artist_id"],))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API — temp folder
# ---------------------------------------------------------------------------

TEMP_ALBUMS_QUERY = """
    SELECT
        a.id, ar.name AS artist, a.title, a.label, a.genre, a.year, a.path,
        a.file_count, a.audio_count, a.cover_path,
        EXISTS (
            SELECT 1 FROM albums other
            JOIN artists ar2 ON other.artist_id = ar2.id
            WHERE other.collection = 1
              AND other.path NOT LIKE :tp || '%'
              AND LOWER(ar2.name) = LOWER(ar.name)
              AND LOWER(other.title) = LOWER(a.title)
        ) AS is_duplicate
    FROM albums a
    JOIN artists ar ON a.artist_id = ar.id
    WHERE a.collection = 1
      AND a.path LIKE :tp || '%'
    ORDER BY ar.name COLLATE NOCASE, a.year DESC, a.title COLLATE NOCASE
"""


def _import_date(path):
    """Extract YYYY-MM-DD from path like TEMP_PATH/2025-01-15/album."""
    rel   = os.path.relpath(path, TEMP_PATH)
    parts = rel.split(os.sep)
    d     = parts[0] if parts else ""
    return d if len(d) == 10 and d.count("-") == 2 else None


@app.route("/api/temp/albums")
def api_temp_albums():
    tp   = TEMP_PATH.rstrip("/") + "/"
    conn = get_db()
    rows = conn.execute(TEMP_ALBUMS_QUERY, {"tp": tp}).fetchall()
    conn.close()
    births = _birth_times([r["path"] for r in rows])
    result = [dict(r) | {"imported": _import_date(r["path"]), "added_at": births.get(r["path"])}
              for r in rows]
    return jsonify(result)


@app.route("/api/temp/stats")
def api_temp_stats():
    tp   = TEMP_PATH.rstrip("/") + "/"
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) FROM albums WHERE collection=1 AND path LIKE ?", (tp + "%",)
    ).fetchone()[0]
    dupes = conn.execute(f"""
        SELECT COUNT(*) FROM ({TEMP_ALBUMS_QUERY}) WHERE is_duplicate=1
    """, {"tp": tp}).fetchone()[0]
    conn.close()
    return jsonify({"total": total, "duplicates": dupes, "unique": total - dupes})


@app.route("/api/temp/albums/<int:album_id>", methods=["DELETE"])
def api_temp_delete(album_id):
    """Delete an album — only allowed if its path is inside TEMP_PATH."""
    tp   = os.path.realpath(TEMP_PATH)
    conn = get_db()
    row  = conn.execute(
        "SELECT id, path, artist_id FROM albums WHERE id=?", (album_id,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Album not found"}), 404

    real_path = os.path.realpath(row["path"])
    if not real_path.startswith(tp + os.sep) and real_path != tp:
        conn.close()
        return jsonify({"error": "Album is not inside the temp folder"}), 403

    try:
        if os.path.exists(row["path"]):
            shutil.rmtree(row["path"])
        conn.execute("DELETE FROM albums WHERE id=?", (album_id,))
        conn.execute("""
            DELETE FROM artists WHERE id=?
              AND id NOT IN (SELECT DISTINCT artist_id FROM albums)
        """, (row["artist_id"],))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API — Airsonic
# ---------------------------------------------------------------------------

@app.route("/api/airsonic/search")
def api_airsonic_search():
    artist = request.args.get("artist", "")
    album  = request.args.get("album", "")
    query  = f"{artist} {album}".strip()
    if not query:
        return jsonify({"error": "Missing query"}), 400
    try:
        data    = airsonic("search3", query=query, albumCount=5, songCount=0, artistCount=0)
        results = data.get("searchResult3", {}).get("album", [])
        albums  = [
            {
                "id":       r.get("id"),
                "name":     r.get("name"),
                "artist":   r.get("artist"),
                "year":     r.get("year"),
                "coverArt": airsonic_coverart_url(r["coverArt"]) if r.get("coverArt") else None,
                "songCount": r.get("songCount"),
            }
            for r in results
        ]
        return jsonify(albums)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/airsonic/coverart")
def api_airsonic_coverart():
    """Proxy cover art images from Airsonic (avoids CORS / auth in browser)."""
    cover_id = request.args.get("id")
    if not cover_id:
        return "", 400
    try:
        url  = f"{AIRSONIC_URL}/rest/getCoverArt.view"
        resp = requests.get(url, params=_airsonic_params(id=cover_id, size=150), timeout=8)
        resp.raise_for_status()
        from flask import Response
        return Response(resp.content, content_type=resp.headers.get("Content-Type", "image/jpeg"))
    except Exception:
        return "", 502


@app.route("/api/airsonic/play", methods=["POST"])
def api_airsonic_play():
    """Play an Airsonic album via jukebox control."""
    data     = request.get_json(silent=True) or {}
    album_id = data.get("albumId")
    if not album_id:
        return jsonify({"error": "Missing albumId"}), 400
    try:
        # Get song IDs from the album
        album_data = airsonic("getAlbum", id=album_id)
        songs      = album_data.get("album", {}).get("song", [])
        song_ids   = [s["id"] for s in songs]
        if not song_ids:
            return jsonify({"error": "Album has no songs"}), 404

        # Set jukebox queue and start playback
        airsonic("jukeboxControl", action="set",  **{f"id": song_ids[0]})
        for sid in song_ids[1:]:
            airsonic("jukeboxControl", action="add", id=sid)
        airsonic("jukeboxControl", action="start")

        return jsonify({"success": True, "tracks": len(song_ids)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/airsonic/playlist/temp", methods=["POST"])
def api_airsonic_playlist_temp():
    """
    Search all temp albums in Airsonic, collect song IDs,
    and create a playlist with them.
    """
    data      = request.get_json(silent=True) or {}
    pl_name   = data.get("name", "Temp")
    tp        = TEMP_PATH.rstrip("/") + "/"
    conn      = get_db()
    rows      = conn.execute(
        "SELECT a.id, ar.name AS artist, a.title FROM albums a "
        "JOIN artists ar ON a.artist_id = ar.id "
        "WHERE a.collection=1 AND a.path LIKE ?",
        (tp + "%",)
    ).fetchall()
    conn.close()

    all_song_ids = []
    not_found    = []

    for row in rows:
        try:
            query   = f"{row['artist']} {row['title']}"
            res     = airsonic("search3", query=query, albumCount=1, songCount=0, artistCount=0)
            albums  = res.get("searchResult3", {}).get("album", [])
            if not albums:
                not_found.append({"artist": row["artist"], "title": row["title"]})
                continue
            album_data = airsonic("getAlbum", id=albums[0]["id"])
            songs      = album_data.get("album", {}).get("song", [])
            all_song_ids.extend(s["id"] for s in songs)
        except Exception:
            not_found.append({"artist": row["artist"], "title": row["title"]})

    if not all_song_ids:
        return jsonify({"error": "No songs found in Airsonic for any temp album",
                        "notFound": not_found}), 404

    try:
        # Build params with multiple songId values
        params = _airsonic_params(name=pl_name)
        for sid in all_song_ids:
            params.setdefault("songId", [])
            if isinstance(params["songId"], list):
                params["songId"].append(sid)

        url  = f"{AIRSONIC_URL}/rest/createPlaylist.view"
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        pl_data = resp.json().get("subsonic-response", {})
        playlist = pl_data.get("playlist", {})

        return jsonify({
            "success":   True,
            "playlistId": playlist.get("id"),
            "name":      playlist.get("name"),
            "songCount": len(all_song_ids),
            "notFound":  not_found,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# API — cover art
# ---------------------------------------------------------------------------

COVER_NAMES = {'cover', 'folder', 'front', 'album', 'artwork', 'art', 'image', 'albumart'}
COVER_EXTS  = {'.jpg', '.jpeg', '.png', '.webp'}


@app.route("/api/albums/cover")
def api_album_cover():
    """
    Serve cover art for an album.
    Priority: local folder image → Last.fm → MusicBrainz CoverArtArchive.
    Query params: path, artist, album, mbid  (all optional)
    """
    from flask import send_file, Response

    path   = request.args.get("path", "")
    artist = request.args.get("artist", "")
    album  = request.args.get("album", "")
    mbid   = request.args.get("mbid", "")

    # 1) Local cover image
    if path and os.path.isdir(path):
        try:
            entries = os.listdir(path)
            entries_lower = {e.lower(): e for e in entries}
            for name in COVER_NAMES:
                for ext in COVER_EXTS:
                    key = name + ext
                    if key in entries_lower:
                        return send_file(os.path.join(path, entries_lower[key]))
            # Any image file
            for e in sorted(entries):
                if os.path.splitext(e)[1].lower() in COVER_EXTS:
                    return send_file(os.path.join(path, e))
        except OSError:
            pass

    # 2) Last.fm
    if artist and album and LASTFM_API_KEY:
        try:
            resp = requests.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "album.getinfo", "artist": artist, "album": album,
                "api_key": LASTFM_API_KEY, "format": "json",
            }, timeout=6)
            images = resp.json().get("album", {}).get("image", [])
            for size in ("extralarge", "large", "medium"):
                url = next((i["#text"] for i in images if i.get("size") == size and i.get("#text")), None)
                if url:
                    img = requests.get(url, timeout=8)
                    img.raise_for_status()
                    return Response(img.content, content_type=img.headers.get("Content-Type", "image/jpeg"))
        except Exception:
            pass

    # 3) MusicBrainz CoverArtArchive
    if mbid:
        try:
            resp = requests.get(
                f"https://coverartarchive.org/release/{mbid}/front-250",
                timeout=8, allow_redirects=True,
                headers={"User-Agent": "TrasvaseCollectionManager/1.0"},
            )
            if resp.status_code == 200:
                return Response(resp.content, content_type=resp.headers.get("Content-Type", "image/jpeg"))
        except Exception:
            pass

    return "", 404


# ---------------------------------------------------------------------------
# API — explore (artists / genres / labels comparison)
# ---------------------------------------------------------------------------

@app.route("/api/explore")
def api_explore():
    view = request.args.get("view", "artists")
    conn = get_db()

    if view == "artists":
        rows = conn.execute("""
            SELECT
                ar.id,
                ar.name,
                COUNT(DISTINCT CASE WHEN a.collection=1 THEN LOWER(a.title) END) AS c1_count,
                COUNT(DISTINCT CASE WHEN a.collection=2 THEN LOWER(a.title) END) AS c2_count
            FROM artists ar
            JOIN albums a ON a.artist_id = ar.id
            GROUP BY ar.id, ar.name
            ORDER BY ar.name COLLATE NOCASE
        """).fetchall()

    elif view == "genres":
        rows = conn.execute("""
            SELECT
                COALESCE(NULLIF(TRIM(genre),''), '(sin género)') AS name,
                COUNT(DISTINCT CASE WHEN collection=1 THEN id END) AS c1_count,
                COUNT(DISTINCT CASE WHEN collection=2 THEN id END) AS c2_count
            FROM albums
            GROUP BY LOWER(COALESCE(NULLIF(TRIM(genre),''), '(sin género)'))
            ORDER BY (c1_count + c2_count) DESC
        """).fetchall()

    elif view == "labels":
        rows = conn.execute("""
            SELECT
                COALESCE(NULLIF(TRIM(label),''), '(sin sello)') AS name,
                COUNT(DISTINCT CASE WHEN collection=1 THEN id END) AS c1_count,
                COUNT(DISTINCT CASE WHEN collection=2 THEN id END) AS c2_count
            FROM albums
            GROUP BY LOWER(COALESCE(NULLIF(TRIM(label),''), '(sin sello)'))
            ORDER BY (c1_count + c2_count) DESC
        """).fetchall()

    else:
        conn.close()
        return jsonify({"error": "Invalid view"}), 400

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/explore/albums")
def api_explore_albums():
    """Albums for a given artist/genre/label, showing in_c1 and in_c2 flags."""
    view = request.args.get("view")
    name = request.args.get("name", "")
    conn = get_db()

    DETAIL_COLS = """
                MAX(a.year)   AS year,
                MAX(a.genre)  AS genre,
                MAX(a.label)  AS label,
                MAX(CASE WHEN a.collection=1 THEN 1    ELSE 0    END) AS in_c1,
                MAX(CASE WHEN a.collection=2 THEN 1    ELSE 0    END) AS in_c2,
                MAX(CASE WHEN a.collection=1 THEN a.id END)           AS id_c1,
                MAX(CASE WHEN a.collection=2 THEN a.id END)           AS id_c2,
                MAX(CASE WHEN a.collection=1 THEN a.path END)         AS path_c1,
                MAX(CASE WHEN a.collection=2 THEN a.path END)         AS path_c2,
                MAX(CASE WHEN a.collection=1 THEN a.file_count  END)  AS files_c1,
                MAX(CASE WHEN a.collection=2 THEN a.file_count  END)  AS files_c2,
                MAX(CASE WHEN a.collection=1 THEN a.audio_count END)  AS audio_c1,
                MAX(CASE WHEN a.collection=2 THEN a.audio_count END)  AS audio_c2,
                MAX(CASE WHEN a.collection=1 THEN a.cover_path  END)  AS cover_c1,
                MAX(CASE WHEN a.collection=2 THEN a.cover_path  END)  AS cover_c2
    """

    if view == "artists":
        rows = conn.execute(f"""
            SELECT
                a.title,
                {DETAIL_COLS}
            FROM albums a
            JOIN artists ar ON a.artist_id = ar.id
            WHERE LOWER(ar.name) = LOWER(:name)
            GROUP BY LOWER(a.title)
            ORDER BY MAX(a.year), a.title COLLATE NOCASE
        """, {"name": name}).fetchall()

    elif view == "genres":
        rows = conn.execute(f"""
            SELECT
                ar.name AS artist,
                a.title,
                {DETAIL_COLS}
            FROM albums a
            JOIN artists ar ON a.artist_id = ar.id
            WHERE LOWER(COALESCE(NULLIF(TRIM(a.genre),''), '(sin género)')) = LOWER(:name)
            GROUP BY ar.id, LOWER(a.title)
            ORDER BY ar.name COLLATE NOCASE, a.year, a.title COLLATE NOCASE
        """, {"name": name}).fetchall()

    elif view == "labels":
        rows = conn.execute(f"""
            SELECT
                ar.name AS artist,
                a.title,
                {DETAIL_COLS}
            FROM albums a
            JOIN artists ar ON a.artist_id = ar.id
            WHERE LOWER(COALESCE(NULLIF(TRIM(a.label),''), '(sin sello)')) = LOWER(:name)
            GROUP BY ar.id, LOWER(a.title)
            ORDER BY ar.name COLLATE NOCASE, a.year, a.title COLLATE NOCASE
        """, {"name": name}).fetchall()

    else:
        conn.close()
        return jsonify({"error": "Invalid view"}), 400

    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True)
