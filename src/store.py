import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("vod-delivery")

DB_PATH = Path("data/vod.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_ARP_CACHE: dict[str, tuple[str | None, float]] = {}
_ARP_TTL_OK = 300.0
_ARP_TTL_MISS = 60.0
_ARP_CACHE_MAX = 256


def _arp_lookup(ip: str) -> str | None:
    import re as _re
    import subprocess

    try:
        subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=3)
        out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if ip in line:
                m = _re.search(r"lladdr\s+([0-9a-fA-F:]{17})", line)
                if m:
                    return m.group(1).lower()
    except Exception:
        pass
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if parts and parts[0] == ip and parts[3] != "00:00:00:00:00:00":
                    return parts[3].lower()
    except Exception:
        pass
    return None


def arp_mac(ip: str) -> str | None:
    now = time.time()
    hit = _ARP_CACHE.get(ip)
    if hit is not None:
        mac, ts = hit
        if now - ts < (_ARP_TTL_OK if mac else _ARP_TTL_MISS):
            return mac
    mac = _arp_lookup(ip)
    if len(_ARP_CACHE) >= _ARP_CACHE_MAX:
        oldest = min(_ARP_CACHE.items(), key=lambda kv: kv[1][1])[0]
        _ARP_CACHE.pop(oldest, None)
    _ARP_CACHE[ip] = (mac, now)
    return mac

_SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    stream_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    lang TEXT,
    subs_lang TEXT,
    year INTEGER,
    category_id INTEGER,
    poster TEXT,
    rating REAL,
    ext TEXT,
    raw_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_movies_title ON movies(title);
CREATE INDEX IF NOT EXISTS idx_movies_lang ON movies(lang);
CREATE TABLE IF NOT EXISTS series (
    series_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    native_title TEXT DEFAULT '',
    lang TEXT,
    year INTEGER,
    category_id INTEGER,
    poster TEXT,
    plot TEXT,
    genre TEXT,
    release_date TEXT,
    rating REAL,
    raw_name TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    episode_id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL,
    season INTEGER,
    episode INTEGER,
    title TEXT,
    ext TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_series ON episodes(series_id, season);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_key TEXT UNIQUE,
    kind TEXT,
    ref_id INTEGER,
    title TEXT,
    series_title TEXT,
    season INTEGER,
    episode INTEGER,
    path TEXT,
    size_bytes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ready',
    source TEXT DEFAULT 'manual',
    downloaded_at TEXT DEFAULT (datetime('now')),
    last_played TEXT,
    source_provider TEXT DEFAULT 'xtream',
    source_id INTEGER,
    retrieved_at TEXT,
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS download_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_key TEXT UNIQUE,
    kind TEXT,
    ref_id INTEGER,
    title TEXT,
    url TEXT,
    ext TEXT,
    source TEXT DEFAULT 'manual',
    status TEXT DEFAULT 'queued',
    error TEXT,
    added_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS tvs (
    udn TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ip TEXT,
    mac TEXT,
    location TEXT,
    kind TEXT DEFAULT 'dlna',
    source TEXT DEFAULT 'discovered',
    wol_enabled INTEGER DEFAULT 0,
    prefer_proxy INTEGER DEFAULT 0,
    downgrade INTEGER DEFAULT 0,
    ignored INTEGER DEFAULT 0,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS banned_tvs (
    mac TEXT PRIMARY KEY,
    label TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    name TEXT,
    created_at TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS media_tracks (
    media_key TEXT PRIMARY KEY,
    audio TEXT,
    subs TEXT,
    probed_at TEXT
);
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    kind TEXT,
    ref_id INTEGER,
    media_key TEXT,
    title TEXT,
    lang TEXT,
    tv_name TEXT,
    started_at TEXT,
    position_s REAL DEFAULT 0,
    duration_s REAL DEFAULT 0,
    updated_at TEXT,
    completed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plays_user ON plays(user_id, started_at DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    title, plot, genre, kind UNINDEXED, ref_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    kind TEXT,
    title TEXT,
    body TEXT
);
CREATE TABLE IF NOT EXISTS app_tokens (
    token TEXT PRIMARY KEY,
    label TEXT,
    created_at TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS pair_requests (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    device_name TEXT,
    platform TEXT,
    instance_id TEXT,
    ip TEXT,
    status TEXT DEFAULT 'pending',
    token TEXT,
    created_at TEXT,
    expires_at TEXT,
    decided_at TEXT,
    claimed_at TEXT
);
CREATE TABLE IF NOT EXISTS watch_progress (
    user_id INTEGER,
    media_key TEXT,
    position_s REAL,
    duration_s REAL,
    updated_at TEXT,
    PRIMARY KEY (user_id, media_key)
);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER,
    kind TEXT,
    ref_id INTEGER,
    title TEXT,
    year INTEGER,
    poster TEXT,
    added_at TEXT,
    PRIMARY KEY (user_id, kind, ref_id)
);
"""


_legacy_token_adopted = False
_fts_checked = False


def connect() -> sqlite3.Connection:
    global _legacy_token_adopted, _fts_checked
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    if not _fts_checked:
        try:
            total = conn.execute("SELECT (SELECT COUNT(*) FROM movies) + (SELECT COUNT(*) FROM series)").fetchone()[0]
            indexed = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
            if indexed != total:
                logger.info("search index out of date (%d of %d) — rebuilding", indexed, total)
                rebuild_search_fts(conn)
        except sqlite3.Error:
            pass
        _fts_checked = True
    if not _legacy_token_adopted:
        try:
            legacy = get_setting(conn, "app_token")
            if legacy:
                conn.execute(
                    "INSERT OR IGNORE INTO app_tokens(token, label, created_at) VALUES(?,?,?)",
                    (legacy, "legacy shared token", _now()),
                )
                conn.commit()
        except sqlite3.Error:
            pass
        _legacy_token_adopted = True
    for table in ("movies", "episodes"):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "subs_lang" not in columns and table == "movies":
            conn.execute("ALTER TABLE movies ADD COLUMN subs_lang TEXT")
        if "probe_subs" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN probe_subs TEXT")
    lib_cols = {r["name"] for r in conn.execute("PRAGMA table_info(library)")}
    for col, ddl in (("subs_path", "TEXT"), ("subs_lang", "TEXT")):
        if col not in lib_cols:
            conn.execute(f"ALTER TABLE library ADD COLUMN {col} {ddl}")
    for col, ddl in (
        ("source_provider", "TEXT DEFAULT 'xtream'"),
        ("source_id", "INTEGER"),
        ("retrieved_at", "TEXT"),
        ("content_hash", "TEXT"),
    ):
        if col not in lib_cols:
            conn.execute(f"ALTER TABLE library ADD COLUMN {col} {ddl}")
    try:
        if conn.execute("SELECT COUNT(*) AS c FROM search_fts").fetchone()["c"] == 0:
            rebuild_search_fts(conn)
    except sqlite3.OperationalError:
        pass
    tv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tvs)")}
    for col in ("kind", "downgrade", "ignored", "v2_seek", "virtual_channel"):
        if col not in tv_cols:
            default = "0" if col in ("downgrade", "ignored", "v2_seek", "virtual_channel") else "'dlna'"
            conn.execute(f"ALTER TABLE tvs ADD COLUMN {col} TEXT DEFAULT {default}" if col == "kind" else f"ALTER TABLE tvs ADD COLUMN {col} INTEGER DEFAULT {default}")
    if "sink_protocols" not in tv_cols:
        conn.execute("ALTER TABLE tvs ADD COLUMN sink_protocols TEXT")
    if "cast_methods" not in tv_cols:
        conn.execute("ALTER TABLE tvs ADD COLUMN cast_methods TEXT")
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def upsert_tv(
    conn: sqlite3.Connection,
    udn: str,
    name: str,
    ip: str | None,
    location: str | None,
    source: str = "discovered",
    mac: str | None = None,
    kind: str = "dlna",
) -> None:
    existing = conn.execute("SELECT * FROM tvs WHERE udn=?", (udn,)).fetchone()
    now = _now()
    if existing:
        conn.execute(
            "UPDATE tvs SET ip=COALESCE(?, ip), location=COALESCE(?, location), mac=COALESCE(?, mac), last_seen=? WHERE udn=?",
            (ip, location, mac, now, udn),
        )
    else:
        conn.execute(
            "INSERT INTO tvs(udn, name, ip, mac, location, source, last_seen, kind) VALUES(?,?,?,?,?,?,?,?)",
            (udn, name, ip, mac, location, source, now, kind),
        )
    if mac and is_banned_mac(conn, mac):
        conn.execute("UPDATE tvs SET ignored=1 WHERE udn=?", (udn,))
    conn.commit()


def find_tv_by_mac(conn: sqlite3.Connection, mac: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tvs WHERE mac=?", (mac,)).fetchone()


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    cleaned = mac.strip().lower().replace("-", ":")
    return cleaned or None


def ban_tv(conn: sqlite3.Connection, mac: str, label: str | None = None) -> None:
    identity = normalize_mac(mac)
    if not identity:
        return
    conn.execute(
        "INSERT INTO banned_tvs(mac, label, created_at) VALUES(?,?,?) "
        "ON CONFLICT(mac) DO UPDATE SET label=COALESCE(excluded.label, banned_tvs.label)",
        (identity, (label or "").strip() or None, _now()),
    )
    conn.execute("UPDATE tvs SET ignored=1 WHERE mac=?", (identity,))
    conn.commit()


def unban_tv(conn: sqlite3.Connection, mac: str) -> None:
    identity = normalize_mac(mac)
    if not identity:
        return
    conn.execute("DELETE FROM banned_tvs WHERE mac=?", (identity,))
    conn.execute("UPDATE tvs SET ignored=0 WHERE mac=?", (identity,))
    conn.commit()


def is_banned_mac(conn: sqlite3.Connection, mac: str | None) -> bool:
    identity = normalize_mac(mac)
    if not identity:
        return False
    return conn.execute("SELECT 1 FROM banned_tvs WHERE mac=?", (identity,)).fetchone() is not None


def upsert_player(conn: sqlite3.Connection, name: str) -> dict:
    import uuid

    row = conn.execute("SELECT * FROM players WHERE name=?", (name,)).fetchone()
    now = _now()
    if row:
        conn.execute("UPDATE players SET last_seen=? WHERE player_id=?", (now, row["player_id"]))
        conn.commit()
        return {"player_id": row["player_id"], "name": row["name"], "last_seen": now}
    player_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO players(player_id, name, created_at, last_seen) VALUES(?,?,?,?)",
        (player_id, name, now, now),
    )
    conn.commit()
    return {"player_id": player_id, "name": name, "last_seen": now}


def get_player(conn: sqlite3.Connection, player_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()


def get_player_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM players WHERE name=?", (name,)).fetchone()


def list_players(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM players ORDER BY last_seen DESC").fetchall()


def touch_player(conn: sqlite3.Connection, player_id: str) -> None:
    conn.execute("UPDATE players SET last_seen=? WHERE player_id=?", (_now(), player_id))
    conn.commit()


def list_banned(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM banned_tvs ORDER BY created_at DESC").fetchall()


def set_media_tracks(conn: sqlite3.Connection, media_key: str, audio: list, subs: list) -> None:
    conn.execute(
        "INSERT INTO media_tracks(media_key, audio, subs, probed_at) VALUES(?,?,?,?) "
        "ON CONFLICT(media_key) DO UPDATE SET audio=excluded.audio, subs=excluded.subs, probed_at=excluded.probed_at",
        (media_key, json.dumps(audio), json.dumps(subs), _now()),
    )
    conn.commit()


def get_media_tracks(conn: sqlite3.Connection, media_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM media_tracks WHERE media_key=?", (media_key,)).fetchone()


def rename_tv(conn: sqlite3.Connection, udn: str, name: str) -> None:
    conn.execute("UPDATE tvs SET name=? WHERE udn=?", (name, udn))
    conn.commit()


def delete_tv(conn: sqlite3.Connection, udn: str) -> None:
    conn.execute("DELETE FROM tvs WHERE udn=?", (udn,))
    conn.commit()


def set_tv_flags(
    conn: sqlite3.Connection,
    udn: str,
    wol_enabled: int | None = None,
    prefer_proxy: int | None = None,
    downgrade: int | None = None,
    mac: str | None = None,
) -> None:
    if wol_enabled is not None:
        conn.execute("UPDATE tvs SET wol_enabled=? WHERE udn=?", (wol_enabled, udn))
    if prefer_proxy is not None:
        conn.execute("UPDATE tvs SET prefer_proxy=? WHERE udn=?", (prefer_proxy, udn))
    if downgrade is not None:
        conn.execute("UPDATE tvs SET downgrade=? WHERE udn=?", (downgrade, udn))
    if mac is not None:
        conn.execute("UPDATE tvs SET mac=? WHERE udn=?", (mac, udn))
    conn.commit()


def set_tv_sink_protocols(conn: sqlite3.Connection, udn: str, payload: dict | None) -> None:
    value = json.dumps(payload) if payload else None
    conn.execute("UPDATE tvs SET sink_protocols=? WHERE udn=?", (value, udn))
    conn.commit()


def set_tv_cast_methods(conn: sqlite3.Connection, udn: str, payload: dict | None) -> None:
    value = json.dumps(payload) if payload else None
    conn.execute("UPDATE tvs SET cast_methods=? WHERE udn=?", (value, udn))
    conn.commit()


def get_tv_cast_methods(conn: sqlite3.Connection, udn: str) -> dict | None:
    row = conn.execute("SELECT cast_methods FROM tvs WHERE udn=?", (udn,)).fetchone()
    if not row or not row["cast_methods"]:
        return None
    try:
        return json.loads(row["cast_methods"])
    except (TypeError, ValueError):
        return None


def find_tv_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tvs WHERE ignored=0 AND name LIKE ? ORDER BY LENGTH(name) LIMIT 1",
        (f"%{name}%",),
    ).fetchone()


def get_tv_sink_protocols(conn: sqlite3.Connection, udn: str) -> dict | None:
    row = conn.execute("SELECT sink_protocols FROM tvs WHERE udn=?", (udn,)).fetchone()
    if not row or not row["sink_protocols"]:
        return None
    try:
        return json.loads(row["sink_protocols"])
    except (TypeError, ValueError):
        return None


def list_tvs(conn: sqlite3.Connection, include_ignored: bool = True) -> list[sqlite3.Row]:
    if include_ignored:
        return conn.execute("SELECT * FROM tvs ORDER BY name").fetchall()
    return conn.execute("SELECT * FROM tvs WHERE ignored=0 ORDER BY name").fetchall()


def get_tv(conn: sqlite3.Connection, udn: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tvs WHERE udn=?", (udn,)).fetchone()


def start_play(conn: sqlite3.Connection, user_id: int, kind: str, ref_id: int, media_key: str, title: str, lang: str | None, tv_name: str) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO plays(user_id, kind, ref_id, media_key, title, lang, tv_name, started_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, kind, ref_id, media_key, title, lang, tv_name, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_play_position(conn: sqlite3.Connection, play_id: int, position_s: float, duration_s: float, completed: bool = False) -> None:
    now = _now()
    conn.execute(
        "UPDATE plays SET position_s=?, duration_s=?, completed=?, updated_at=? WHERE id=?",
        (position_s, duration_s, int(completed), now, play_id),
    )
    conn.commit()


def get_progress(conn: sqlite3.Connection, user_id: int, media_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM watch_progress WHERE user_id=? AND media_key=?", (user_id, media_key)
    ).fetchone()


def set_progress(conn: sqlite3.Connection, user_id: int, media_key: str, position_s: float, duration_s: float) -> None:
    if not media_key:
        return
    now = _now()
    conn.execute(
        "INSERT INTO watch_progress(user_id, media_key, position_s, duration_s, updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id, media_key) DO UPDATE SET position_s=excluded.position_s, duration_s=excluded.duration_s, updated_at=excluded.updated_at",
        (user_id, media_key, position_s, duration_s, now),
    )
    conn.commit()


def recent_plays(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM plays ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()


def continue_watching(conn: sqlite3.Connection, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM watch_progress WHERE user_id=? AND position_s > 30 AND (duration_s = 0 OR position_s < duration_s * 0.95) "
        "ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def add_watchlist(conn: sqlite3.Connection, user_id: int, kind: str, ref_id: int, title: str, year: int | None, poster: str) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO watchlist(user_id, kind, ref_id, title, year, poster, added_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, kind, ref_id) DO NOTHING",
        (user_id, kind, ref_id, title, year, poster, now),
    )
    conn.commit()


def remove_watchlist(conn: sqlite3.Connection, user_id: int, kind: str, ref_id: int) -> None:
    conn.execute("DELETE FROM watchlist WHERE user_id=? AND kind=? AND ref_id=?", (user_id, kind, ref_id))
    conn.commit()


def list_watchlist(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM watchlist WHERE user_id=? ORDER BY added_at DESC", (user_id,)).fetchall()


def progress_title(conn: sqlite3.Connection, media_key: str) -> str:
    if not media_key:
        return ""
    kind, _, ref = media_key.partition(":")
    if not ref:
        return media_key
    if kind == "movie":
        try:
            row = conn.execute("SELECT title FROM movies WHERE stream_id=?", (int(ref),)).fetchone()
        except ValueError:
            return media_key
    elif kind == "episode":
        try:
            episode_id = int(ref.split(":")[0])
        except ValueError:
            return media_key
        row = conn.execute(
            "SELECT e.title, e.season, e.episode, s.title AS series_title FROM episodes e "
            "JOIN series s ON s.series_id = e.series_id WHERE e.episode_id=?", (episode_id,)
        ).fetchone()
        if row:
            base = f"{row['series_title']} — S{row['season']}E{row['episode']}"
            return f"{base} · {row['title']}" if row["title"] and not row["title"].startswith("S") else base
        return media_key
    else:
        try:
            row = conn.execute("SELECT title FROM series WHERE series_id=?", (int(ref),)).fetchone()
        except ValueError:
            return media_key
    return row["title"] if row else media_key


def upsert_library(
    conn: sqlite3.Connection, media_key: str, kind: str, ref_id: int, title: str,
    path: str, size_bytes: int, series_title: str | None = None,
    season: int | None = None, episode: int | None = None, source: str = "manual",
    source_provider: str = "xtream", retrieved_at: str | None = None,
    content_hash: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO library(media_key, kind, ref_id, title, series_title, season, episode, path,"
        " size_bytes, status, source, source_provider, source_id, retrieved_at, content_hash)"
        " VALUES(?,?,?,?,?,?,?,?,?, 'ready', ?, ?, ?, ?, ?)"
        " ON CONFLICT(media_key) DO UPDATE SET path=excluded.path, size_bytes=excluded.size_bytes,"
        " status='ready', downloaded_at=datetime('now'), retrieved_at=excluded.retrieved_at,"
        " content_hash=COALESCE(excluded.content_hash, library.content_hash)",
        (media_key, kind, ref_id, title, series_title, season, episode, path, size_bytes, source,
         source_provider, ref_id, retrieved_at, content_hash),
    )
    conn.commit()


def set_library_status(conn: sqlite3.Connection, media_key: str, status: str, path: str | None = None) -> None:
    if path is not None:
        conn.execute("UPDATE library SET status=?, path=? WHERE media_key=?", (status, path, media_key))
    else:
        conn.execute("UPDATE library SET status=? WHERE media_key=?", (status, media_key))
    conn.commit()


def get_library_item(conn: sqlite3.Connection, media_key: str):
    return conn.execute("SELECT * FROM library WHERE media_key=?", (media_key,)).fetchone()


def list_library(conn: sqlite3.Connection, statuses: tuple = ("ready", "archived")):
    rows = conn.execute(
        "SELECT * FROM library ORDER BY COALESCE(series_title, title), season, episode"
    ).fetchall()
    return [r for r in rows if r["status"] in statuses]


def library_size_bytes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS s FROM library WHERE status='ready'").fetchone()
    return row["s"]


def enqueue_download(conn: sqlite3.Connection, media_key: str, kind: str, ref_id: int,
                     title: str, url: str, ext: str, source: str = "watchlist") -> bool:
    existing = conn.execute("SELECT id, status FROM download_queue WHERE media_key=?", (media_key,)).fetchone()
    lib = get_library_item(conn, media_key)
    if lib is not None and lib["status"] in ("ready", "archived"):
        return False
    if existing is not None and existing["status"] in ("queued", "downloading"):
        return False
    conn.execute(
        "INSERT INTO download_queue(media_key, kind, ref_id, title, url, ext, source) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(media_key) DO UPDATE SET status='queued', error=NULL",
        (media_key, kind, ref_id, title, url, ext, source),
    )
    conn.commit()
    return True


def dequeue_next(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM download_queue WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()


def set_queue_status(conn: sqlite3.Connection, media_key: str, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE download_queue SET status=?, error=? WHERE media_key=?", (status, error, media_key))
    conn.commit()


def list_queue(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM download_queue ORDER BY id DESC LIMIT 50").fetchall()


def remove_queue_item(conn: sqlite3.Connection, media_key: str) -> None:
    conn.execute("DELETE FROM download_queue WHERE media_key=?", (media_key,))
    conn.commit()


_AR_TRANSLATE = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي",
    "ـ": "", "\u064b": "", "\u064c": "", "\u064d": "",
    "\u064e": "", "\u064f": "", "\u0650": "", "\u0651": "",
    "\u0652": "", "\u0670": "",
})


def normalize_arabic(text: str) -> str:
    return (text or "").translate(_AR_TRANSLATE)


def strip_arabic_article(word: str) -> str:
    for prefix in ("وال", "بال", "كال", "فال", "لل", "ال"):
        if word.startswith(prefix) and len(word) > len(prefix) + 1:
            return word[len(prefix):]
    return word


def search_variants(text: str) -> str:
    norm = normalize_arabic(text or "")
    return " ".join(strip_arabic_article(w) for w in norm.split())


def _search_text(title, native) -> str:
    parts = [title or "", native or ""]
    combined = " ".join(p for p in parts if p)
    parts.append(search_variants(combined))
    seen = []
    for part in parts:
        for word in part.split():
            if word not in seen:
                seen.append(word)
    return " ".join(seen)


def rebuild_search_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM search_fts")
    movies = conn.execute("SELECT stream_id, title FROM movies").fetchall()
    conn.executemany(
        "INSERT INTO search_fts(title, plot, genre, kind, ref_id) VALUES(?,?,?,?,?)",
        [(_search_text(r["title"], None), "", "", "movie", r["stream_id"]) for r in movies],
    )
    series = conn.execute(
        "SELECT series_id, title, native_title, plot, genre FROM series"
    ).fetchall()
    conn.executemany(
        "INSERT INTO search_fts(title, plot, genre, kind, ref_id) VALUES(?,?,?,?,?)",
        [
            (_search_text(r["title"], r["native_title"]), r["plot"] or "", r["genre"] or "", "series", r["series_id"])
            for r in series
        ],
    )
    conn.commit()


def add_event(conn: sqlite3.Connection, kind: str, title: str, body: str = "") -> None:
    conn.execute(
        "INSERT INTO events(ts, kind, title, body) VALUES(?,?,?,?)",
        (_now(), kind, title, body),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, since: int = 0, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (since, limit)
    ).fetchall()


def create_app_token(conn: sqlite3.Connection, token: str, label: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_tokens(token, label, created_at) VALUES(?,?,?)",
        (token, label, _now()),
    )
    conn.commit()


def valid_app_token(conn: sqlite3.Connection, token: str) -> bool:
    row = conn.execute("SELECT token FROM app_tokens WHERE token=?", (token,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE app_tokens SET last_seen=? WHERE token=?", (_now(), token))
    conn.commit()
    return True


def list_app_tokens(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT token, label, created_at, last_seen FROM app_tokens ORDER BY created_at DESC"
    ).fetchall()


def revoke_app_token(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM app_tokens WHERE token=?", (token,))
    conn.commit()


PAIR_TTL_S = 600


def issue_app_token(conn: sqlite3.Connection, label: str = "") -> str:
    import secrets

    token = secrets.token_urlsafe(24)
    create_app_token(conn, token, label)
    return token


def _expire_pair_requests(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE pair_requests SET status='expired' WHERE status='pending' AND expires_at<?",
        (_now(),),
    )
    conn.commit()


def create_pair_request(
    conn: sqlite3.Connection,
    device_name: str,
    platform: str = "",
    instance_id: str = "",
    ip: str = "",
) -> dict:
    import secrets

    request_id = secrets.token_hex(12)
    code = secrets.token_hex(4).upper()
    created_at = _now()
    expires_at = datetime.fromtimestamp(
        time.time() + PAIR_TTL_S, timezone.utc
    ).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO pair_requests(id, code, device_name, platform, instance_id, ip, status, created_at, expires_at) "
        "VALUES(?,?,?,?,?,?,'pending',?,?)",
        (
            request_id,
            code,
            (device_name or "")[:80],
            (platform or "")[:40],
            (instance_id or "")[:80],
            (ip or "")[:45],
            created_at,
            expires_at,
        ),
    )
    conn.commit()
    return {"id": request_id, "code": code, "expires_at": expires_at}


def get_pair_request(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    _expire_pair_requests(conn)
    return conn.execute(
        "SELECT * FROM pair_requests WHERE id=?", (request_id,)
    ).fetchone()


def list_pair_requests(conn: sqlite3.Connection, status: str = "pending") -> list[sqlite3.Row]:
    _expire_pair_requests(conn)
    return conn.execute(
        "SELECT * FROM pair_requests WHERE status=? ORDER BY created_at DESC", (status,)
    ).fetchall()


def count_pending_pairs(conn: sqlite3.Connection) -> int:
    _expire_pair_requests(conn)
    return conn.execute(
        "SELECT COUNT(*) AS c FROM pair_requests WHERE status='pending'"
    ).fetchone()["c"]


def approve_pair_request(conn: sqlite3.Connection, request_id: str) -> str | None:
    _expire_pair_requests(conn)
    row = conn.execute(
        "SELECT * FROM pair_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None or row["status"] != "pending":
        return None
    label = (row["device_name"] or "device").strip()
    if row["platform"]:
        label = f"{label} · {row['platform']}"
    token = issue_app_token(conn, label[:80])
    conn.execute(
        "UPDATE pair_requests SET status='approved', token=?, decided_at=? WHERE id=?",
        (token, _now(), request_id),
    )
    conn.commit()
    return token


def deny_pair_request(conn: sqlite3.Connection, request_id: str) -> bool:
    _expire_pair_requests(conn)
    cur = conn.execute(
        "UPDATE pair_requests SET status='denied', decided_at=? WHERE id=? AND status='pending'",
        (_now(), request_id),
    )
    conn.commit()
    return cur.rowcount > 0


def pair_status(conn: sqlite3.Connection, request_id: str, code: str) -> dict | None:
    import hmac

    _expire_pair_requests(conn)
    row = conn.execute(
        "SELECT * FROM pair_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None or not code or not hmac.compare_digest(str(row["code"]), str(code)):
        return None
    if row["status"] == "approved" and row["token"]:
        token = row["token"]
        conn.execute(
            "UPDATE pair_requests SET status='claimed', token=NULL, claimed_at=? WHERE id=?",
            (_now(), request_id),
        )
        conn.commit()
        return {"status": "approved", "token": token}
    return {"status": row["status"]}
