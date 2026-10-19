import asyncio
import json
import re

FFMPEG = "/usr/bin/ffmpeg"

_LANG_RE = re.compile(r"#\d+:\d+\((\w{2,3})\)")


async def probe_subtitles(media_url: str, timeout: float = 20.0) -> list[str]:
    from .config import PROVIDER_USER_AGENT

    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-user_agent", PROVIDER_USER_AGENT, "-i", media_url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    langs: list[str] = []
    for line in stderr.decode("utf-8", "replace").splitlines():
        if "Subtitle:" not in line:
            continue
        m = _LANG_RE.search(line)
        code = m.group(1).lower() if m else "und"
        if code not in langs:
            langs.append(code)
    return langs


async def probe_and_cache(kind: str, ref_id: int, media_url: str) -> list[str]:
    langs = await probe_subtitles(media_url)
    if not langs:
        return []
    from . import store

    conn = store.connect()
    try:
        table = "movies" if kind == "movie" else "episodes"
        id_col = "stream_id" if kind == "movie" else "episode_id"
        row = conn.execute(f"SELECT probe_subs FROM {table} WHERE {id_col}=?", (ref_id,)).fetchone()
        existing = json.loads(row["probe_subs"]) if row and row["probe_subs"] else []
        merged = existing + [c for c in langs if c not in existing]
        conn.execute(f"UPDATE {table} SET probe_subs=? WHERE {id_col}=?", (json.dumps(merged), ref_id))
        conn.commit()
        return merged
    finally:
        conn.close()
