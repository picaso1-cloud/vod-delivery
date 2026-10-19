import logging
import re
import struct
from pathlib import Path

import httpx

from .config import OPENSUBTITLES_API_KEY

logger = logging.getLogger(__name__)
API = "https://api.opensubtitles.com/api/v1"
_TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def _headers() -> dict:
    return {"Api-Key": OPENSUBTITLES_API_KEY, "User-Agent": "vod-delivery v1", "Accept": "application/json"}


def os_hash(path: Path) -> str:
    filesize = path.stat().st_size
    chksum = filesize
    with path.open("rb") as fh:
        for pos in (0, max(0, filesize - 65536)):
            fh.seek(pos)
            chunk = fh.read(65536)
            for i in range(0, len(chunk) - len(chunk) % 8, 8):
                chksum = (chksum + struct.unpack("<Q", chunk[i:i + 8])[0]) & 0xFFFFFFFFFFFFFFFF
    return f"{chksum:016x}"


def srt_to_vtt(data: bytes) -> bytes:
    text = data.decode("utf-8", "replace").lstrip("\ufeff")
    text = _TS_RE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    if not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
    return text.encode("utf-8")


def _score(attrs: dict, duration_s: float | None) -> int:
    score = 0
    if attrs.get("ai_translated"):
        score -= 2
    if attrs.get("hearing_impaired"):
        score -= 1
    if attrs.get("foreign_parts_only"):
        score -= 3
    if attrs.get("format") == "srt":
        score += 1
    runtime = (attrs.get("feature_details") or {}).get("runtime")
    if duration_s and runtime:
        if abs(runtime * 60 - duration_s) > 180:
            score -= 5
    return score


def _search(client: httpx.Client, params: dict, duration_s: float | None) -> list[dict]:
    r = client.get(f"{API}/subtitles", params=params, headers=_headers())
    if r.status_code != 200:
        logger.warning("opensubtitles search HTTP %s params=%s", r.status_code, list(params))
        return []
    out = []
    for item in r.json().get("data") or []:
        attrs = item.get("attributes") or {}
        files = attrs.get("files") or []
        if files and files[0].get("file_id"):
            out.append({"score": _score(attrs, duration_s), "attrs": attrs, "file_id": files[0]["file_id"]})
    return out


def hunt_srt(title: str, year: int | None = None, duration_s: float | None = None,
             languages: tuple = ("ar",), file_path: Path | None = None, timeout: float = 40) -> dict:
    if not OPENSUBTITLES_API_KEY:
        return {"ok": False, "reason": "no OPENSUBTITLES_API_KEY"}
    attempts = []
    if file_path is not None:
        attempts.append({"moviehash": os_hash(file_path), "languages": ",".join(languages), "_duration_s": duration_s})
    q = {"query": title, "languages": ",".join(languages), "_duration_s": duration_s}
    if year:
        q["year"] = year
    attempts.append(q)
    candidates: list[dict] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for params in attempts:
            want_duration = params.pop("_duration_s", None)
            candidates = _search(client, params, want_duration)
            if candidates:
                break
        if not candidates:
            return {"ok": False, "reason": "no subtitles found"}
        candidates.sort(key=lambda c: -c["score"])
        best = candidates[0]
        dr = client.post(f"{API}/download", json={"file_id": best["file_id"]}, headers=_headers())
        if dr.status_code != 200:
            return {"ok": False, "reason": f"download HTTP {dr.status_code}"}
        link = (dr.json() or {}).get("link")
        if not link:
            return {"ok": False, "reason": "no download link"}
        fr = client.get(link)
        if fr.status_code != 200 or not fr.content:
            return {"ok": False, "reason": f"file HTTP {fr.status_code}"}
    content = fr.content[:2_000_000]
    return {"ok": True, "lang": languages[0], "release": best["attrs"].get("release") or "", "srt": content}
