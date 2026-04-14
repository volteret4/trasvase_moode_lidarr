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

import requests
from flask import Flask, jsonify, request, render_template

from config import (
    AIRSONIC_PASSWORD, AIRSONIC_URL, AIRSONIC_USER,
    COLLECTION_1_PATH, COLLECTION_2_PATH, DB_PATH,
    FLASK_HOST, FLASK_PORT, TEMP_PATH,
)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


MISSING_QUERY = """
    SELECT a2.id, ar.name AS artist, a2.title, a2.label, a2.genre, a2.year, a2.path
    FROM albums a2
    JOIN artists ar ON a2.artist_id = ar.id
    WHERE a2.collection = 2
      AND NOT EXISTS (
          SELECT 1 FROM albums a1
          JOIN artists ar1 ON a1.artist_id = ar1.id
          WHERE a1.collection = 1
            AND LOWER(ar1.name) = LOWER(ar.name)
            AND LOWER(a1.title) = LOWER(a2.title)
      )
    ORDER BY ar.name COLLATE NOCASE, a2.year DESC, a2.title COLLATE NOCASE
"""


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

@app.route("/api/stats")
def api_stats():
    conn = get_db()
    stats = {
        "collection1": conn.execute("SELECT COUNT(*) FROM albums WHERE collection=1").fetchone()[0],
        "collection2": conn.execute("SELECT COUNT(*) FROM albums WHERE collection=2").fetchone()[0],
        "artists":     conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0],
        "missing":     conn.execute(f"SELECT COUNT(*) FROM ({MISSING_QUERY})").fetchone()[0],
    }
    conn.close()
    return jsonify(stats)


@app.route("/api/albums/missing")
def api_missing():
    conn = get_db()
    rows = conn.execute(MISSING_QUERY).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


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

        src_path = row["path"]
        rel      = os.path.relpath(src_path, COLLECTION_2_PATH)
        dst_path = os.path.join(COLLECTION_1_PATH, rel)

        if os.path.exists(dst_path):
            results.append({"id": album_id, "success": False, "error": "Destination already exists"})
            continue

        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copytree(src_path, dst_path)

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


@app.route("/api/temp/albums")
def api_temp_albums():
    tp   = TEMP_PATH.rstrip("/") + "/"
    conn = get_db()
    rows = conn.execute(TEMP_ALBUMS_QUERY, {"tp": tp}).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


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

if __name__ == "__main__":
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True)
