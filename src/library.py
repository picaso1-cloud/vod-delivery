import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import store

logger = logging.getLogger("vod-delivery")

LIBRARY_DIR = Path("data/library")
ARCHIVE_DIR = Path("data/archive")
INBOX_DIR = Path("data/inbox")
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv", ".webm", ".mpg", ".mpeg", ".flv"}


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", (name or "")).strip().rstrip(".").strip() or "Unknown"


def _ext(payload_ext: str) -> str:
    ext = (payload_ext or "mkv").lower().lstrip(".")
    return "mkv" if ext in ("m3u8", "ts") else ext


def movie_rel_path(title: str, year, ext: str) -> Path:
    name = _safe_name(f"{title} ({year})" if year else title)
    return Path("movies") / f"{name}.{_ext(ext)}"


def episode_rel_path(series_title: str, season: int, episode: int, ep_name: str, ext: str) -> Path:
    show = _safe_name(series_title)
    label = f"S{int(season):02d}E{int(episode):02d}"
    if ep_name and not ep_name.upper().startswith("S0"):
        label += f" - {_safe_name(ep_name)}"
    return Path("shows") / show / f"{label}.{_ext(ext)}"


def abs_path(rel: Path) -> Path:
    return LIBRARY_DIR / rel


def tmp_path(token: str, ext: str) -> Path:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_DIR / f".tmp-{token}.{_ext(ext)}"
    return tmp


def finalize_download(tmp: Path, rel: Path) -> Path:
    final = abs_path(rel)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        final.unlink()
    shutil.move(str(tmp), str(final))
    return final


def delete_media(media_key: str) -> bool:
    conn = store.connect()
    try:
        item = store.get_library_item(conn, media_key)
        if item is None:
            return False
        path = Path(item["path"]) if item["path"] else None
        if item["status"] == "ready" and path and path.exists():
            path.unlink()
            parent = path.parent
            if parent.name not in ("movies", "shows", str(LIBRARY_DIR)) and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        store.set_library_status(conn, media_key, "online_only")
        return True
    finally:
        conn.close()


def archive_media(media_key: str, archive_root: Path | None = None) -> tuple[bool, str]:
    conn = store.connect()
    try:
        item = store.get_library_item(conn, media_key)
        if item is None or item["status"] != "ready":
            return False, "not available locally"
        src = Path(item["path"])
        if not src.exists():
            store.set_library_status(conn, media_key, "online_only")
            return False, "file missing — marked online-only"
        root = archive_root or ARCHIVE_DIR
        rel = src.relative_to(LIBRARY_DIR)
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        store.set_library_status(conn, media_key, "archived", str(dest))
        return True, str(dest)
    finally:
        conn.close()


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def cache_usage_bytes() -> int:
    return dir_size(Path("data/cache/streams")) + library_size_all()


def library_size_all() -> int:
    conn = store.connect()
    try:
        return store.library_size_bytes(conn)
    finally:
        conn.close()


async def sync_watchlist_queue() -> int:
    conn = store.connect()
    added = 0
    try:
        items = conn.execute("SELECT kind, ref_id, title FROM watchlist ORDER BY rowid").fetchall()
        for it in items:
            kind, ref_id = it["kind"], int(it["ref_id"])
            if kind == "series":
                eps = await _episodes_to_download(conn, ref_id, keep_ahead=2)
                for ep in eps:
                    ep_row = conn.execute(
                        "SELECT e.season, e.episode, e.ext, s.title AS series_title, s.series_id"
                        " FROM episodes e JOIN series s ON s.series_id=e.series_id WHERE e.episode_id=?",
                        (ep,),
                    ).fetchone()
                    if ep_row is None:
                        continue
                    key = f"episode:{ep}:{ep_row['series_id']}"
                    lib = conn.execute("SELECT status FROM library WHERE media_key=?", (key,)).fetchone()
                    if lib is not None and lib["status"] in ("ready", "archived"):
                        continue
                    label = f"{ep_row['series_title']} — S{ep_row['season']}E{ep_row['episode']}"
                    if _enqueue_item(conn, "episode", ep, label):
                        added += 1
                continue
            if kind == "episode":
                ep_row = conn.execute(
                    "SELECT e.season, e.episode, e.ext, s.title AS series_title, s.series_id"
                    " FROM episodes e JOIN series s ON s.series_id=e.series_id WHERE e.episode_id=?",
                    (ref_id,),
                ).fetchone()
                if ep_row is None:
                    continue
                key = f"episode:{ref_id}:{ep_row['series_id']}"
                lib = conn.execute("SELECT status FROM library WHERE media_key=?", (key,)).fetchone()
                if lib is not None and lib["status"] in ("ready", "archived"):
                    continue
                label = f"{ep_row['series_title']} — S{ep_row['season']}E{ep_row['episode']}"
                if _enqueue_item(conn, "episode", ref_id, label):
                    added += 1
            elif kind == "movie":
                lib = conn.execute("SELECT status FROM library WHERE media_key=?", (f"movie:{ref_id}",)).fetchone()
                if lib is not None and lib["status"] in ("ready", "archived"):
                    continue
                if _enqueue_item(conn, "movie", ref_id, it["title"]):
                    added += 1
        return added
    finally:
        conn.close()


async def _episodes_to_download(conn, series_id: int, keep_ahead: int = 2) -> list[int]:
    from .provider import Provider

    row = conn.execute("SELECT title FROM series WHERE series_id=?", (series_id,)).fetchone()
    if row is None:
        return []
    last_watched = None
    for r in conn.execute(
        "SELECT media_key FROM watch_progress WHERE media_key LIKE 'episode:%' ORDER BY updated_at DESC LIMIT 30"
    ).fetchall():
        _, _, ref = r["media_key"].partition(":")
        ep_id, _, sref = ref.partition(":")
        if sref and int(sref) == series_id:
            erow = conn.execute("SELECT season, episode FROM episodes WHERE episode_id=?", (int(ep_id),)).fetchone()
            if erow is not None:
                last_watched = (erow["season"], erow["episode"])
                break
    last_downloaded = None
    for r in conn.execute("SELECT media_key, status FROM library WHERE media_key LIKE 'episode:%'").fetchall():
        if r["status"] not in ("ready", "archived"):
            continue
        _, _, ref = r["media_key"].partition(":")
        ep_id, _, sref = ref.partition(":")
        if sref and int(sref) == series_id:
            erow = conn.execute("SELECT season, episode FROM episodes WHERE episode_id=?", (int(ep_id),)).fetchone()
            if erow is not None:
                pos = (erow["season"], erow["episode"])
                if last_downloaded is None or pos > last_downloaded:
                    last_downloaded = pos
    cursor = max((c for c in (last_watched, last_downloaded) if c is not None), default=None)
    try:
        info = await Provider().series_info(series_id)
    except Exception:
        logger.exception("series_info failed for %s", series_id)
        return []
    eps = sorted(
        ((int(sk), int(e.get("episode_num", 0)), int(e["id"])) for sk, el in info.get("episodes", {}).items() for e in el),
        key=lambda x: (x[0], x[1]),
    )
    if cursor is None:
        return [i_ for _, _, i_ in eps[:keep_ahead]]
    return [i_ for s_, n_, i_ in eps if (s_, n_) > cursor][:keep_ahead]


def _enqueue_item(conn, kind: str, ref_id: int, title: str) -> bool:
    from .provider import Provider

    provider = Provider()
    try:
        if kind == "movie":
            row = conn.execute("SELECT title, year, ext FROM movies WHERE stream_id=?", (ref_id,)).fetchone()
            if row is None:
                return False
            key = f"movie:{ref_id}"
            url = provider.movie_url(ref_id, row["ext"])
            label = f"{row['title']} ({row['year'] or ''})".strip()
            return store.enqueue_download(conn, key, kind, ref_id, label, url, row["ext"], source="watchlist")

        episode_id = int(ref_id)
        row = conn.execute(
            "SELECT e.season, e.episode, e.ext, s.title AS series_title, s.series_id"
            " FROM episodes e JOIN series s ON s.series_id=e.series_id WHERE e.episode_id=?",
            (episode_id,),
        ).fetchone()
        if row is None:
            return False
        key = f"episode:{episode_id}:{row['series_id']}"
        url = provider.episode_url(episode_id, row["ext"])
        label = f"{row['series_title']} — S{row['season']}E{row['episode']}"
        return store.enqueue_download(conn, key, kind, episode_id, label, url, row["ext"], source="watchlist")
    except Exception:
        logger.exception("enqueue item failed: %s %s", kind, ref_id)
        return False


def plan_for(kind: str, ref_id: int, ext: str) -> dict | None:
    conn = store.connect()
    try:
        if kind == "movie":
            row = conn.execute("SELECT title, year FROM movies WHERE stream_id=?", (ref_id,)).fetchone()
            if row is None:
                return None
            year = row["year"]
            title = f"{row['title']} ({year})" if year else row["title"]
            return {
                "media_key": f"movie:{ref_id}",
                "rel": movie_rel_path(title, year, ext),
                "title": title,
                "series_title": None, "season": None, "episode": None,
            }
        row = conn.execute(
            "SELECT e.season, e.episode, e.title AS ep_title, s.title AS series_title, s.series_id"
            " FROM episodes e JOIN series s ON s.series_id=e.series_id WHERE e.episode_id=?",
            (ref_id,),
        ).fetchone()
        if row is None:
            return None
        rel = episode_rel_path(row["series_title"], row["season"], row["episode"], row["ep_title"], ext)
        return {
            "media_key": f"episode:{ref_id}:{row['series_id']}",
            "rel": rel,
            "title": f"{row['series_title']} — S{row['season']}E{row['episode']}",
            "series_title": row["series_title"], "season": row["season"], "episode": row["episode"],
        }
    finally:
        conn.close()


def _file_hash(path: Path) -> str | None:
    import hashlib
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def register_download(media_key: str, kind: str, ref_id: int, title: str, rel: Path,
                      size_bytes: int, plan: dict, source: str) -> None:
    full = abs_path(rel)
    conn = store.connect()
    try:
        store.upsert_library(
            conn, media_key, kind, ref_id, title, str(full), size_bytes,
            series_title=plan.get("series_title"), season=plan.get("season"),
            episode=plan.get("episode"), source=source,
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            content_hash=_file_hash(full),
        )
    finally:
        conn.close()


def owner_chat_id() -> int | None:
    conn = store.connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='allowed_ids'").fetchone()
    finally:
        conn.close()
    if not row or not row["value"].strip():
        return None
    import re as _re
    ids = _re.findall(r"\d+", row["value"])
    return int(ids[0]) if ids else None


def cache_limit_bytes() -> int:
    conn = store.connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='cache_limit_gb'").fetchone()
    finally:
        conn.close()
    try:
        return int(float(row["value"]) * 1024**3) if row and row["value"] else 50 * 1024**3
    except (TypeError, ValueError):
        return 50 * 1024**3


def register_personal(title: str, rel: Path, size_bytes: int) -> str:
    tmp_key = f"personal:pending:{time.time_ns()}"
    conn = store.connect()
    try:
        store.upsert_library(
            conn, tmp_key, "personal", 0, title, str(abs_path(rel)), size_bytes,
            source="manual", source_provider="manual",
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        row = conn.execute("SELECT id FROM library WHERE media_key=?", (tmp_key,)).fetchone()
        key = f"personal:{row['id']}"
        conn.execute("UPDATE library SET media_key=? WHERE id=?", (key, row["id"]))
        conn.commit()
        return key
    finally:
        conn.close()


def _ffprobe_ok(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["/usr/bin/ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0 and "video" in (out.stdout or ""):
            return {"ok": True}
        return {"ok": False, "reason": "no video stream"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "probe timeout"}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__}


def _unique_dest(title: str, suffix: str) -> Path:
    base = _safe_name(title)
    dest = abs_path(Path("movies") / f"{base}{suffix}")
    n = 2
    while dest.exists():
        dest = abs_path(Path("movies") / f"{base} ({n}){suffix}")
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def scan_inbox() -> dict:
    from . import parsing

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    imported, skipped = [], []
    for f in sorted(INBOX_DIR.iterdir()):
        if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in VIDEO_EXTS:
            continue
        probe = _ffprobe_ok(f)
        if not probe["ok"]:
            skipped.append((f.name, probe["reason"]))
            continue
        title, year = parsing.guess_title_from_filename(f.stem)
        if year:
            title = f"{title} ({year})"
        suffix = f.suffix.lower()
        dest = _unique_dest(title, suffix)
        shutil.move(str(f), str(dest))
        rel = dest.relative_to(LIBRARY_DIR)
        key = register_personal(title, rel, dest.stat().st_size)
        logger.info("inbox import: %s -> %s [%s]", f.name, dest.name, key)
        imported.append((key, title))
    return {"imported": imported, "skipped": skipped}


def list_ready(limit: int = 10, offset: int = 0) -> tuple[list, int]:
    conn = store.connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM library WHERE status='ready' AND path IS NOT NULL"
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT media_key, title, size_bytes, kind, downloaded_at FROM library"
            " WHERE status='ready' AND path IS NOT NULL ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()
    return rows, total


URL_UA = "VOD-delivery/1.0"
CONTENT_EXT = {
    "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "video/mp2t": ".ts", "video/quicktime": ".mov", "video/x-msvideo": ".avi",
    "video/mpeg": ".mpg",
}


async def probe_media_url(url: str) -> dict:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            r = await client.head(url, headers={"User-Agent": URL_UA})
            if r.status_code >= 400 or r.status_code < 200:
                r = await client.get(url, headers={"User-Agent": URL_UA, "Range": "bytes=0-0"})
                if r.status_code >= 400:
                    return {"ok": False, "reason": f"HTTP {r.status_code}"}
            if "text/html" in (r.headers.get("content-type") or "").lower():
                return {"ok": False, "reason": "page, not a media file"}
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            size = 0
            cr = r.headers.get("content-range") or ""
            if "/" in cr:
                try:
                    size = int(cr.rsplit("/", 1)[1])
                except ValueError:
                    size = 0
            if not size:
                try:
                    size = int(r.headers.get("content-length") or 0)
                except ValueError:
                    size = 0
            cd = r.headers.get("content-disposition") or ""
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";\r\n]+)\"?", cd)
            name = m.group(1) if m else url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
            name = name.strip() or "video"
            if "." not in name and ctype in CONTENT_EXT:
                name += CONTENT_EXT[ctype]
            return {"ok": True, "title_source": name, "size": size, "content_type": ctype or "application/octet-stream"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}


async def download_url(url: str, dest: Path, progress=None, max_seconds: int = 4 * 3600) -> dict:
    import httpx

    deadline = asyncio.get_running_loop().time() + max_seconds
    backoff = 1.0
    last_t = 0.0
    pos = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        while True:
            if asyncio.get_running_loop().time() > deadline:
                return {"ok": False, "reason": "timeout"}
            pos = part.stat().st_size if part.exists() else 0
            interrupted = False
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    headers = {"User-Agent": URL_UA}
                    if pos > 0:
                        headers["Range"] = f"bytes={pos}-"
                    async with client.stream("GET", url, headers=headers) as r:
                        if r.status_code == 200 and pos > 0:
                            logger.warning("url server ignored Range (200 at %s) — restarting clean", pos)
                            part.write_bytes(b"")
                            pos = 0
                        if r.status_code not in (200, 206):
                            return {"ok": False, "reason": f"HTTP {r.status_code}"}
                        remaining = int(r.headers.get("content-length") or 0)
                        total = pos + remaining
                        with part.open("ab" if pos else "wb") as fh:
                            async for chunk in r.aiter_bytes(512 * 1024):
                                fh.write(chunk)
                                pos += len(chunk)
                                backoff = 1.0
                                if progress and time.time() - last_t >= 5:
                                    last_t = time.time()
                                    await progress(pos, total)
            except Exception as exc:
                logger.warning("url download interrupted at %s: %s — resuming", pos, type(exc).__name__)
                interrupted = True
            if interrupted:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            break
        if progress:
            await progress(pos, pos)
        final = shutil.move(str(part), str(dest))
        return {"ok": True, "path": Path(final), "size": Path(final).stat().st_size}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


SUBS_DIR = LIBRARY_DIR / "subs"
FFMPEG = "/usr/bin/ffmpeg"


def _subs_cache_path(media_key: str, tag: str) -> Path:
    return SUBS_DIR / f"{media_key.replace(':', '_')}.{tag}.srt"


def _set_subs(media_key: str, subs_path: str | None, subs_lang: str | None) -> None:
    conn = store.connect()
    try:
        conn.execute("UPDATE library SET subs_path=?, subs_lang=? WHERE media_key=?", (subs_path, subs_lang, media_key))
        conn.commit()
    finally:
        conn.close()


def _ffprobe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def _extract_first_srt(path: Path, out: Path) -> bool:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", str(path), "-map", "0:s:0", "-c:s", "srt", str(out)],
            capture_output=True, timeout=300,
        )
        return r.returncode == 0 and out.exists() and out.stat().st_size > 0
    except Exception:
        return False
    finally:
        if not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)


def _mux_srt(path: Path, srt: Path) -> bool:
    tmp = path.with_suffix(".submux.tmp.mkv")
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", str(path), "-i", str(srt),
             "-map", "0", "-map", "1", "-c", "copy", "-c:s", "srt",
             "-metadata:s:s:0", "language=ara", str(tmp)],
            capture_output=True, timeout=900,
        )
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > path.stat().st_size * 0.5:
            os.replace(tmp, path)
            return True
    except Exception:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return False


def ensure_subs(media_key: str) -> dict:
    conn = store.connect()
    try:
        item = store.get_library_item(conn, media_key)
    finally:
        conn.close()
    if item is None or not item["path"]:
        return {"ok": False, "reason": "item not in library"}
    if item["subs_lang"]:
        return {"ok": True, "cached": True, "lang": item["subs_lang"]}
    path = Path(item["path"])
    if not path.exists():
        return {"ok": False, "reason": "file missing"}
    from .subs import probe_subtitles
    try:
        langs = asyncio.run(probe_subtitles(str(path)))
    except Exception:
        langs = []
    if "ara" in langs or "ar" in langs:
        _set_subs(media_key, None, "embedded")
        return {"ok": True, "lang": "embedded"}
    title, year = item["title"], None
    m = re.search(r"\((\d{4})\)", title or "")
    if m:
        year = int(m.group(1))
        title = title[:m.start()].strip()
    from . import osubs
    hunt = osubs.hunt_srt(title, year=year, duration_s=_ffprobe_duration(path), languages=("ar",), file_path=path)
    if not hunt["ok"]:
        if hunt["reason"] == "no subtitles found":
            _set_subs(media_key, None, "none")
        else:
            return {"ok": False, "reason": hunt["reason"], "transient": True}
        return {"ok": False, "reason": hunt["reason"]}
    sp = _subs_cache_path(media_key, hunt["lang"])
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_bytes(hunt["srt"])
    if path.suffix.lower() == ".mkv" and _mux_srt(path, sp):
        sp.unlink(missing_ok=True)
        _set_subs(media_key, None, "ara")
        return {"ok": True, "lang": "ara-muxed"}
    _set_subs(media_key, str(sp), hunt["lang"])
    return {"ok": True, "lang": hunt["lang"], "sidecar": True}


def subs_status(media_key: str) -> str:
    conn = store.connect()
    try:
        row = conn.execute("SELECT subs_path, subs_lang FROM library WHERE media_key=?", (media_key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return ""
    lang = row["subs_lang"]
    if lang in (None, ""):
        return ""
    if lang == "none":
        return "⚠️ no subs found for this one"
    if lang == "embedded":
        return "🌐 subs: ara (embedded)"
    if lang == "ara":
        return "🌐 subs: ara (muxed)"
    return f"🌐 subs: {lang} (sidecar)"


def get_subs_for_stream(media_key: str) -> bytes | None:
    conn = store.connect()
    try:
        item = store.get_library_item(conn, media_key)
    finally:
        conn.close()
    if item is None:
        return None
    lang = item["subs_lang"]
    if not lang or lang == "none":
        return None
    if item["subs_path"] and Path(item["subs_path"]).exists():
        return Path(item["subs_path"]).read_bytes()
    if item["path"] and Path(item["path"]).exists():
        cache = _subs_cache_path(media_key, "x")
        if cache.exists() and cache.stat().st_size > 0:
            return cache.read_bytes()
        if _extract_first_srt(Path(item["path"]), cache):
            return cache.read_bytes()
    return None
