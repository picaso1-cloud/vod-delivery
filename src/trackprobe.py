import asyncio
import json
import logging

from . import store
from .config import PROVIDER_USER_AGENT

logger = logging.getLogger("vod-delivery")

FFPROBE = "/usr/bin/ffprobe"
PROBE_TIMEOUT = 25.0


def parse_streams(streams: list[dict]) -> dict:
    audio = []
    subs = []
    for s in streams:
        kind = s.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        entry = {
            "index": int(s.get("index", -1)),
            "codec": s.get("codec_name"),
            "lang": (tags.get("language") or "").lower() or None,
            "title": (tags.get("title") or "").strip() or None,
            "default": bool(disp.get("default")),
        }
        if kind == "audio":
            entry["channels"] = s.get("channels")
            audio.append(entry)
        else:
            entry["forced"] = bool(disp.get("forced"))
            subs.append(entry)
    return {"audio": audio, "subs": subs}


async def probe(url: str) -> dict:
    cmd = [
        FFPROBE, "-v", "error",
        "-user_agent", PROVIDER_USER_AGENT,
        "-show_entries",
        "stream=index,codec_type,codec_name,channels:stream_tags=language,title:stream_disposition=default,forced",
        "-of", "json",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        logger.warning("track probe timed out for %s", url.rsplit("/", 1)[-1])
        return {"audio": [], "subs": []}
    except Exception as exc:
        logger.warning("track probe failed (%s)", type(exc).__name__)
        return {"audio": [], "subs": []}
    if not out.strip():
        logger.info("track probe empty (%s)", err.decode("utf-8", "replace")[-120:].strip())
        return {"audio": [], "subs": []}
    try:
        streams = json.loads(out).get("streams", [])
    except json.JSONDecodeError:
        return {"audio": [], "subs": []}
    return parse_streams(streams)


def _has_active_session() -> bool:
    from . import proxy

    return proxy.active_session() is not None


async def probe_and_cache(media_key: str, url: str, force: bool = False) -> dict:
    if not force:
        cached = get_cached(media_key)
        if cached:
            return cached
    if _has_active_session():
        logger.info("track probe deferred — provider slot busy (%s)", media_key)
        return {"audio": [], "subs": [], "deferred": True}
    tracks = await probe(url)
    conn = store.connect()
    try:
        store.set_media_tracks(conn, media_key, tracks["audio"], tracks["subs"])
    finally:
        conn.close()
    if not tracks["audio"] and not tracks["subs"]:
        return {**tracks, "empty": True}
    logger.info("track probe %s: %d audio, %d subs", media_key, len(tracks["audio"]), len(tracks["subs"]))
    return tracks


def get_cached(media_key: str, retry_empty_after: float = 3600.0) -> dict | None:
    conn = store.connect()
    try:
        row = store.get_media_tracks(conn, media_key)
    finally:
        conn.close()
    if not row:
        return None
    try:
        data = {
            "audio": json.loads(row["audio"] or "[]"),
            "subs": json.loads(row["subs"] or "[]"),
            "probed_at": row["probed_at"],
        }
    except json.JSONDecodeError:
        return None
    if not data["audio"] and not data["subs"]:
        try:
            from datetime import datetime, timezone

            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["probed_at"])).total_seconds()
        except Exception:
            age = retry_empty_after + 1
        if age < retry_empty_after:
            return {**data, "empty": True}
        return None
    return data
