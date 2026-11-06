# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Music collection sync tool with two web interfaces:

- **`/`** — Trasvase: shows albums in Collection 2 missing from Collection 1, select and copy them
- **`/temp`** — Temp manager: shows albums inside `coleccion1/temp/`, highlights duplicates, integrates with Airsonic

## Setup

```bash
pip install -r requirements.txt

# Edit config.py or set env vars (actual paths come from .encrypted.env via sops):
export COLLECTION_1_PATH=/ruta/al/nas/moode/moode
export COLLECTION_2_PATH=/ruta/al/nas/lidar/torrents_backup
export TEMP_PATH=/ruta/al/nas/moode/moode/temp/auto-dietpi  # default: COLLECTION_1_PATH/temp/auto-dietpi

# Airsonic (optional, for /temp page):
export AIRSONIC_URL=http://localhost:4040
export AIRSONIC_USER=admin
export AIRSONIC_PASSWORD=secret

python scan.py                # scan & sync both collections into music.db
python scan.py --musicbrainz  # also enrich via MusicBrainz (slow, 1 req/s)

python app.py                 # start Flask on 0.0.0.0:5000
```

## File Structure

- **`config.py`** — all paths and credentials, env-var overrideable
- **`scan.py`** — walks both collection folders, reads FLAC tags via mutagen, upserts into `music.db`; handles initial creation and incremental sync (add new, prune deleted)
- **`app.py`** — Flask: serves both pages + all REST API routes
- **`templates/index.html`** — Trasvase SPA
- **`templates/temp.html`** — Temp manager SPA

## Database Schema (`music.db`, SQLite)

```sql
artists(id, name UNIQUE COLLATE NOCASE, mbid)
albums(id, artist_id FK→artists, title, label, genre, year, path UNIQUE, collection 1|2, mbid)
```

One `artists` row per unique name (case-insensitive). Albums in `temp/` are stored with `collection=1`; they're identified at query time by `path LIKE TEMP_PATH || '%'`.

## API Routes

| Method   | Path                          | Description                                                |
|----------|-------------------------------|------------------------------------------------------------|
| GET      | `/api/stats`                  | Counts for both collections, artists, missing              |
| GET      | `/api/albums/missing`         | Albums in C2 with no matching artist+title in C1, with `added_at` (folder creation time, or mtime if older for bulk-copied albums; epoch, null if unknown) |
| POST     | `/api/albums/copy`            | `{ids:[…]}` — copy C2 → `TEMP_PATH/YYYY-MM-DD/album`      |
| DELETE   | `/api/albums/<id>`            | Delete album from disk + DB                                |
| GET      | `/api/temp/albums`            | Temp albums with `is_duplicate` flag and `added_at` (folder creation time, epoch; null if unknown) |
| GET      | `/api/temp/stats`             | `{total, duplicates, unique}` for temp folder              |
| DELETE   | `/api/temp/albums/<id>`       | Delete from temp only (403 if path escapes TEMP_PATH)      |
| GET      | `/api/explore`                | `?view=artists\|genres\|labels` — aggregate counts C1/C2   |
| GET      | `/api/explore/albums`         | `?view=…&name=…` — albums for a group with in_c1/in_c2     |
| GET      | `/api/airsonic/search`        | `?artist=&album=` — proxy search to Subsonic API           |
| GET      | `/api/airsonic/coverart`      | `?id=` — proxy cover art image (avoids browser CORS)       |
| POST     | `/api/airsonic/play`          | `{albumId}` — jukebox set+start via Subsonic API           |
| POST     | `/api/airsonic/playlist/temp` | `{name}` — search all temp albums in Airsonic, create playlist |

## Key Logic Notes

- **Duplicate detection**: an album in `temp/` is a duplicate when `LOWER(artist.name)` + `LOWER(album.title)` match any C1 album outside temp. Path is irrelevant.
- **Airsonic auth**: uses Subsonic token-based auth (`t=md5(password+salt)&s=salt`).
- **Playlist creation**: searches each temp album in Airsonic via `search3`, fetches song IDs via `getAlbum`, then calls `createPlaylist` with all song IDs in one request.
- **Metadata priority**: mutagen FLAC tags → folder name fallback → optional MusicBrainz enrichment.
