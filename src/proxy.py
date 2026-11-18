import asyncio
import logging
import os
import re
import ipaddress
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from aiohttp import web

from .config import PROVIDER_USER_AGENT
from . import store
from .library import LIBRARY_DIR
from .ssdp import local_ipv4s

logger = logging.getLogger("vod-delivery")

PROXY_PORT = 8091
CACHE_DIR = Path("data/cache/streams")


@dataclass
class ProxySession:
    token: str
    provider_url: str
    title: str
    mode: str = "copy"
    mime: str = "video/mpeg"
    path: Path | None = None
    proc: asyncio.subprocess.Process | None = None
    clients: int = 0
    started_at: float = 0.0
    idle_timeout_s: float = 180.0
    declare_current: bool = False
    output: bytes = field(default=b"")
    done: bool = False
    first_chunk: bytes | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    total: int | None = None
    start_offset: float = 0.0
    dest_rel: Path | None = None
    completed: bool = False
    resume_byte: int = 0
    duration_s: float = 0.0
    max_served: int = 0
    est_length: int | None = None
    seek_target_s: float | None = None
    feed_content_s: float | None = None
    muxer_proc: asyncio.subprocess.Process | None = None
    decoder_content_base: float | None = None
    decoder_ts_base: float = 0.0
    last_emitted_pts: float = 0.0
    virtual_transcode: bool = False
    virtual_loop: bool = False
    respawn_count: int = 0
    decoder_bytes: int = 0
    seek_marks: list = field(default_factory=list)
    feed_generation: int = 0
    last_client_seen: float = 0.0
    stderr_log: object | None = None
    is_live: bool = False
    stdout_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    live_file: Path | None = None
    deferred: bool = False
    deferred_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tv_name: str | None = None
    audio_index: int | None = None
    sub_index: int | None = None


_sessions: dict[str, ProxySession] = {}
_lock = asyncio.Lock()
FFMPEG = "/usr/bin/ffmpeg"


def active_session() -> ProxySession | None:
    for session in _sessions.values():
        if not session.done:
            return session
    return None


def get_session(token: str) -> ProxySession | None:
    return _sessions.get(token)


def list_sessions() -> list[ProxySession]:
    live = [s for s in _sessions.values() if not s.done]
    return sorted(live, key=lambda s: s.started_at, reverse=True)


def progress_seconds(session: ProxySession) -> tuple[float, float] | None:
    if session.mode == "virtual":
        if session.feed_content_s is None or session.decoder_content_base is None:
            return None
        pos = session.decoder_content_base + (session.feed_content_s - session.decoder_ts_base)
        dur = session.duration_s or 0
        return (min(pos, dur) if dur > 0 else pos), dur
    if session.is_live or session.duration_s <= 0 or not session.total:
        return None
    if session.seek_marks:
        served = session.max_served
        marks = session.seek_marks
        avg_bps = session.total / session.duration_s if session.duration_s else 0.0
        pos = marks[0][1]
        for idx, (mark_bytes, mark_time) in enumerate(marks):
            if served < mark_bytes:
                break
            pos = mark_time
            if avg_bps > 0:
                pos += (served - mark_bytes) / avg_bps
        return min(pos, session.duration_s), session.duration_s
    span = max(1, session.total - session.resume_byte)
    frac = min(1.0, session.max_served / span)
    pos = session.start_offset + frac * (session.duration_s - session.start_offset)
    return min(pos, session.duration_s), session.duration_s


async def seek_session(token: str, new_pos: float) -> tuple[bool, str]:
    session = _sessions.get(token)
    if session is None:
        return False, "no such session"
    if session.mode == "virtual":
        ok = await seek_virtual(session, new_pos)
        return ok, "virtual decoder swap"
    if session.is_live:
        return False, "live sessions cannot seek"
    if session.mode == "cache_mp4":
        return False, "mp4 sessions are not server-seekable"
    if session.duration_s <= 0:
        return False, "duration unknown — cannot seek this stream"
    new_pos = max(0.0, min(new_pos, session.duration_s - 30))
    async with _lock:
        session.feed_generation += 1
        session.seek_target_s = new_pos
        keep = min(session.max_served, _file_size(session))
        session.seek_marks.append((keep, new_pos))
        if session.proc is not None and session.proc.returncode is None:
            try:
                session.proc.kill()
            except ProcessLookupError:
                pass
        if session.path is not None:
            with session.path.open("r+b") as fh:
                fh.truncate(keep)
            session.first_chunk = None
        if session.mode == "cache":
            asyncio.create_task(_cache_feed_task(session, transcode=False, my_generation=session.feed_generation))
        elif session.mode == "cache_downgrade":
            asyncio.create_task(_cache_feed_task(session, transcode=True, my_generation=session.feed_generation))
        else:
            return False, f"seek not supported for mode {session.mode}"
    logger.info("seek_session: repositioning %s to %.0fs (append at %s bytes)", token, new_pos, _file_size(session))
    for _ in range(20):
        await asyncio.sleep(1.5)
        if session.proc is not None and session.proc.returncode is None:
            return True, "feed repositioned"
    return False, "feed did not restart"


def lan_ip_for(target_ip: str) -> str | None:
    prefix = ".".join(target_ip.split(".")[:3])
    for ip in local_ipv4s():
        if ".".join(ip.split(".")[:3]) == prefix:
            return ip
    return local_ipv4s()[0] if local_ipv4s() else None


def proxied_url(token: str, target_ip: str) -> str:
    return f"http://{lan_ip_for(target_ip)}:{PROXY_PORT}/stream/{token}"


async def _kill_orphan_ffmpeg() -> None:
    import subprocess as sp

    try:
        sp.run(["pkill", "-f", "[d]ata/cache/streams"], capture_output=True, timeout=5)
    except Exception:
        pass


async def start_session(
    provider_url: str, title: str, mode: str = "copy", mime: str = "video/mpeg",
    start_offset: float = 0.0, dest_rel: Path | None = None, live: bool = False,
    declare_current: bool = False, deferred: bool = False,
    audio_index: int | None = None, sub_index: int | None = None,
) -> ProxySession:
    await _kill_orphan_ffmpeg()
    if audio_index is not None and mode == "cache":
        mode = "cache_remux"
        mime = "video/x-matroska"
    async with _lock:
        for old in _sessions.values():
            await _kill(old)
        _sessions.clear()
        await _ensure_disk_space()
        session = ProxySession(
            token=uuid.uuid4().hex[:12], provider_url=provider_url, title=title,
            mode=mode, mime=mime, start_offset=start_offset, dest_rel=dest_rel, is_live=live,
            declare_current=declare_current, audio_index=audio_index, sub_index=sub_index,
        )
        if mode == "hls":
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            session.path = CACHE_DIR / session.token
            session.path.mkdir(parents=True, exist_ok=True)
        elif mode.startswith("cache"):
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            suffix = ".mp4" if mode == "cache_mp4" else (".ts" if mime == "video/mpeg" else ".mkv")
            session.path = CACHE_DIR / f"{session.token}{suffix}"
        session.deferred = bool(deferred)
        session.started_at = asyncio.get_running_loop().time()
        session.last_client_seen = session.started_at
        _sessions[session.token] = session
    if session.is_live:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        session.live_file = CACHE_DIR / f"{session.token}.ts"
    if mode.startswith("cache"):
        if not (deferred and declare_current and start_offset <= 0):
            session.total = await _probe_total(provider_url)
        session.seek_marks.append((0, start_offset))
        if mode in ("cache_downgrade", "cache_mp4", "cache_remux") and not deferred:
            session.duration_s = await _probe_duration(provider_url)
            if session.duration_s > 0:
                session.total = int(session.duration_s * 550_000)
        if not live and start_offset > 0:
            if not session.duration_s:
                session.duration_s = await _probe_duration(provider_url)
            if session.duration_s > 0 and session.total:
                frac = min(0.98, start_offset / session.duration_s)
                session.resume_byte = int(session.total * frac)
                logger.info("resume: will start at %.0fs (est byte %d of %s) for %s", start_offset, session.resume_byte, session.total, title[:40])
            else:
                logger.warning("resume: ffprobe failed for %s — starting from 0", title[:40])
    if deferred and mode.startswith("cache") and not live:
        asyncio.create_task(_session_watchdog(session))
        logger.info("session %s prepared (deferred until first fetch) — %s", session.token, title[:40])
        return session
    await _spawn_ffmpeg(session)
    if session.is_live:
        asyncio.create_task(_live_file_task(session))
        ready = False
        for _ in range(40):
            if session.done:
                break
            if session.live_file and session.live_file.exists() and session.live_file.stat().st_size >= 400 * 1024:
                ready = True
                break
            await asyncio.sleep(0.25)
        if not ready:
            await _kill(session)
            raise RuntimeError(
                "live source unavailable — channel offline, not in subscription, "
                "or provider slot busy (retry in ~45s)"
            )
    asyncio.create_task(_prime(session))
    if not mode.startswith("cache"):
        asyncio.create_task(_session_watchdog(session))
    return session


async def _live_file_task(session: ProxySession) -> None:
    respawns = 0
    appended = 0
    appended_bytes = 0
    started = asyncio.get_running_loop().time()
    try:
        while not session.done:
            if appended_bytes > MAX_LIVE_FILE_BYTES:
                logger.warning("live file hit %.1fGB cap — ending session %s", MAX_LIVE_FILE_BYTES / 1024**3, session.token)
                break
            if asyncio.get_running_loop().time() - started > MAX_LIVE_SECONDS:
                logger.info("live session reached max duration — ending %s", session.token)
                break
            proc = session.proc
            if proc is None or proc.stdout is None:
                break
            chunk = await proc.stdout.read(256 * 1024)
            if not chunk:
                if session.done:
                    break
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                respawns += 1
                if appended == 0 and respawns >= 5:
                    logger.warning("live source never produced data (%s, channel file %s) — offline, not in subscription, or provider slot busy", session.token, session.provider_url.rsplit("/", 1)[-1])
                    break
                if respawns > 300:
                    logger.warning("live appender giving up after 300 respawns (%s)", session.token)
                    break
                await asyncio.sleep(1.0 if session.clients <= 0 else 0.3)
                await _spawn_ffmpeg(session)
                continue
            appended += 1
            appended_bytes += len(chunk)
            if session.live_file:
                with session.live_file.open("ab") as fh:
                    fh.write(chunk)
    except asyncio.CancelledError:
        pass
    finally:
        session.done = True


async def _session_watchdog(session: ProxySession) -> None:
    cap = _session_max_bytes()
    while not session.done:
        await asyncio.sleep(30)
        if session.path is not None and session.path.exists():
            try:
                size = sum(f.stat().st_size for f in session.path.rglob("*") if f.is_file()) if session.path.is_dir() else session.path.stat().st_size
            except OSError:
                size = 0
            if size > cap:
                logger.error("session %s exceeded the %.1f GB cap (%.1f GB) — killing", session.token, cap / 1024**3, size / 1024**3)
                await _kill(session)
                return
        limit = session.idle_timeout_s or 180
        if asyncio.get_running_loop().time() - session.last_client_seen > limit:
            logger.info("session idle (no client for %.0fs) — reaping: %s", limit, session.title)
            await _kill(session)
            return


async def _feed_progress_reader(rd: int, session: ProxySession) -> None:
    fh = os.fdopen(rd, "rb", buffering=0)
    try:
        while True:
            line = await asyncio.to_thread(fh.readline)
            if not line:
                break
            key = line.split(b"=", 1)
            if len(key) != 2:
                continue
            name, val = key[0], key[1].strip()
            if name in (b"out_time_us", b"out_time_ms"):
                try:
                    session.feed_content_s = max(0.0, int(val) / 1_000_000.0)
                    session.last_emitted_pts = session.feed_content_s
                except ValueError:
                    pass
    except Exception:
        pass
    finally:
        try:
            fh.close()
        except Exception:
            pass


def _decoder_codec_args(session: ProxySession, transcode: bool) -> list:
    if not transcode:
        return ["-c", "copy"]
    return [
        "-vf", "scale=w=1920:h=1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
    ]


async def _spawn_virtual_decoder(session: ProxySession, content_time: float, transcode: bool) -> None:
    offset = session.last_emitted_pts + 0.2
    pr, pw = os.pipe()
    http_src = session.provider_url.startswith("http")
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
        "-re",
        "-progress", f"pipe:{pw}",
        *(["-stream_loop", "-1"] if session.virtual_loop else []),
        "-ss", f"{content_time:.3f}",
        *(["-user_agent", PROVIDER_USER_AGENT] if http_src else []),
        "-i", session.provider_url,
        *_decoder_codec_args(session, transcode),
        *_map_args(session, "mpegts"),
        "-copytb", "1", "-output_ts_offset", f"{offset:.3f}",
        "-f", "mpegts", "pipe:1",
    ]
    err_path = session.path.parent / f"vdec-{session.token}.err"
    err_fh = open(err_path, "wb")
    session.stderr_log = err_fh
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=err_fh, pass_fds=(pw,))
    os.close(pw)
    session.proc = proc
    session.decoder_content_base = content_time
    session.decoder_ts_base = offset
    session.last_emitted_pts = offset
    asyncio.create_task(_feed_progress_reader(pr, session))
    asyncio.create_task(_pump_decoder_to_file(session, proc))


VIRTUAL_MAX_BYTES = 4 * 1024 ** 3
MIN_FREE_BYTES = 10 * 1024 ** 3
DEFAULT_SESSION_MAX_GB = 8.0


def _disk_free(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _session_max_bytes() -> int:
    conn = store.connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='session_max_gb'").fetchone()
    finally:
        conn.close()
    try:
        gb = float(row["value"]) if row and row["value"] else DEFAULT_SESSION_MAX_GB
    except (TypeError, ValueError):
        gb = DEFAULT_SESSION_MAX_GB
    return int(gb * 1024 ** 3)


def _purge_inactive_cache() -> int:
    if not CACHE_DIR.exists():
        return 0
    active = {s.path.name for s in _sessions.values() if s.path is not None}
    freed = 0
    now = time.time()
    for entry in list(CACHE_DIR.iterdir()):
        if entry.name in active:
            continue
        try:
            if now - entry.stat().st_mtime < 60:
                continue
            if entry.is_dir():
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                shutil.rmtree(entry, ignore_errors=True)
            else:
                size = entry.stat().st_size
                entry.unlink(missing_ok=True)
            freed += size
        except OSError:
            continue
    return freed


async def _ensure_disk_space() -> None:
    base = CACHE_DIR if CACHE_DIR.exists() else Path(".")
    free = _disk_free(base)
    if free >= MIN_FREE_BYTES:
        return
    freed = _purge_inactive_cache()
    free = _disk_free(base)
    if freed:
        logger.warning("purged %.1f GB of stale cache (free now %.1f GB)", freed / 1024**3, free / 1024**3)
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"disk full: {free / 1024**3:.1f} GB free (need {MIN_FREE_BYTES / 1024**3:.0f} GB)")


async def _pump_decoder_to_file(session: ProxySession, proc: asyncio.subprocess.Process) -> None:
    try:
        with session.path.open("ab") as fh:
            while True:
                chunk = await muxer.stdout.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                fh.flush()
        await muxer.wait()
    except Exception:
        logger.exception("virtual muxer pump failed (%s)", session.token)
    finally:
        try:
            muxer.stdout.close()
        except Exception:
            pass


async def _pump_decoder_to_file(session: ProxySession, proc: asyncio.subprocess.Process) -> None:
    try:
        with session.path.open("ab") as fh:
            while True:
                chunk = await proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                fh.flush()
                session.decoder_bytes += len(chunk)
                if session.path.stat().st_size > VIRTUAL_MAX_BYTES:
                    logger.warning("virtual file hit 4GB cap — ending session %s", session.token)
                    asyncio.create_task(stop_session(session.token))
                    return
        await proc.wait()
    except Exception:
        logger.exception("virtual decoder pump failed (%s)", session.token)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass


async def _virtual_channel_task(session: ProxySession, transcode: bool, my_generation: int) -> None:
    while not session.done and session.feed_generation == my_generation:
        base = session.decoder_content_base
        if session.seek_target_s is not None:
            base = session.seek_target_s
            session.seek_target_s = None
        elif session.feed_content_s and session.decoder_content_base is not None:
            base = session.decoder_content_base + (session.feed_content_s - session.decoder_ts_base)
        session.decoder_content_base = base
        session.feed_content_s = None
        session.decoder_bytes = 0
        await _spawn_virtual_decoder(session, base, transcode)
        await session.proc.wait()
        if session.done or session.feed_generation != my_generation:
            break
        logger.warning("virtual decoder exited (%s) at content %.0fs — respawning", session.token, base)
        await asyncio.sleep(min(20, 5 * session.respawn_count) if session.decoder_bytes == 0 else min(10, 2 * session.respawn_count))
        session.respawn_count += 1


async def start_library_session(path: Path, title: str, mime: str) -> ProxySession:
    token = uuid.uuid4().hex[:12]
    size = path.stat().st_size
    session = ProxySession(
        token=token, provider_url="", title=title, mode="library", mime=mime,
        path=path, total=size,
    )
    session.ready.set()
    _sessions[token] = session
    logger.info("library session %s ready: %s (%d bytes)", token, title[:40], size)
    return session


async def start_virtual_session(provider_url: str, title: str, transcode: bool, start_offset: float = 0.0, loop: bool = False, idle_timeout: float = 180.0, declare_current: bool = False, audio_index: int | None = None) -> ProxySession:
    async with _lock:
        for old_session in list(_sessions.values()):
            if not old_session.done:
                await _kill(old_session)
        _sessions.clear()
        await _ensure_disk_space()
    token = uuid.uuid4().hex[:12]
    path = CACHE_DIR / f"virtual-{token}.ts"
    session = ProxySession(
        token=token, provider_url=provider_url, title=title, mode="virtual",
        mime="video/mpeg", path=path, start_offset=start_offset,
        audio_index=audio_index,
    )
    _sessions[token] = session
    session.virtual_transcode = transcode
    session.virtual_loop = loop
    session.idle_timeout_s = idle_timeout
    session.declare_current = declare_current
    session.decoder_content_base = float(start_offset)
    session.duration_s = await _probe_duration(provider_url)
    if transcode:
        if session.duration_s > 0:
            session.total = int(session.duration_s * 550_000)
    else:
        probed = await _probe_total(provider_url)
        if probed:
            session.total = probed
    if loop:
        session.total = VIRTUAL_MAX_BYTES
    asyncio.create_task(_virtual_channel_task(session, transcode, session.feed_generation))
    deadline = asyncio.get_running_loop().time() + 45
    while asyncio.get_running_loop().time() < deadline:
        if session.done:
            raise RuntimeError("virtual source unavailable")
        if path.exists() and path.stat().st_size >= 400 * 1024:
            break
        await asyncio.sleep(0.25)
    else:
        await _kill(session)
        raise RuntimeError("virtual session failed to prime")
    logger.info("virtual channel started (%s) at %.0fs — %s", token, start_offset, title[:40])
    return session


async def seek_virtual(session: ProxySession, new_pos: float) -> bool:
    session.feed_generation += 1
    gen = session.feed_generation
    old = session.proc
    if old and old.returncode is None:
        old.kill()
        await old.wait()
    session.seek_target_s = max(0.0, new_pos)
    session.respawn_count = 0
    asyncio.create_task(_virtual_channel_task(session, session.virtual_transcode, gen))
    logger.info("virtual seek to %.0fs (%s) — decoder swapped, muxer untouched", new_pos, session.token)
    return True


async def _probe_tail_time(session: ProxySession) -> float | None:
    if not session.path or not session.path.exists() or session.path.stat().st_size < 100_000:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ffprobe", "-v", "error", "-read_intervals", "99999999%+#1",
            "-select_streams", "v:0", "-show_entries", "packet=pts_time",
            "-of", "default=nw=1:nk=1", str(session.path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        lines = [x for x in out.decode().strip().splitlines() if x and x != "N/A"]
        if not lines:
            return None
        return max(0.0, float(lines[-1]))
    except Exception:
        return None


def _map_args(session: ProxySession, container: str) -> list:
    if session.audio_index is None and session.sub_index is None:
        return []
    args = ["-map", "0:v:0"]
    if session.audio_index is not None:
        args += ["-map", f"0:{session.audio_index}"]
    else:
        args += ["-map", "0:a:0?"]
    if session.sub_index is not None and container == "matroska":
        args += ["-map", f"0:{session.sub_index}", "-c:s", "copy"]
    return args


async def _cache_feed_task(session: ProxySession, transcode: bool, my_generation: int,
                           container: str = "mpegts", extra: list | None = None,
                           map_args: list | None = None) -> None:
    total = session.total or 0
    avg_bps = (total / session.duration_s) if session.duration_s else 0.0
    session.stderr_log = (session.path.parent / f"ffmpeg-{session.token}.log").open("wb") if session.path else None
    respawn = 0
    spawn_bytes = 0
    zero_progress = 0
    last_target_s = session.start_offset
    while not session.done and session.feed_generation == my_generation:
        produced_s = (_file_size(session) / avg_bps) if avg_bps else 0.0
        if session.seek_target_s is not None:
            target_s = session.seek_target_s
            session.seek_target_s = None
            session.feed_content_s = None
        elif session.feed_content_s:
            target_s = session.feed_content_s
        else:
            tail = await _probe_tail_time(session)
            if tail is not None:
                target_s = (last_target_s + tail) if spawn_bytes > 0 else last_target_s
            else:
                end_byte = _file_size(session)
                base_s, base_b = session.start_offset, 0
                for b, t in sorted(session.seek_marks):
                    if b <= end_byte:
                        base_s, base_b = t, b
                produced_s = ((end_byte - base_b) / avg_bps) if avg_bps else 0.0
                target_s = min(base_s + produced_s, (session.duration_s or 0) * 0.98)
        if session.duration_s:
            target_s = min(target_s, max(0.0, session.duration_s * 0.98))
        if transcode:
            codec_args = [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-vf", "scale=w=1920:h=1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            ]
        else:
            codec_args = ["-c", "copy"]
        pr, pw = os.pipe()
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", f"pipe:{pw}",
            "-ss", f"{target_s:.3f}", "-user_agent", PROVIDER_USER_AGENT,
            "-i", session.provider_url,
            *codec_args, *(map_args or []), *(extra or []), "-f", container, "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, pass_fds=(pw,))
        except Exception:
            logger.warning("resume ffmpeg spawn failed for %s", session.title[:40], exc_info=True)
            break
        session.proc = proc
        os.close(pw)
        last_target_s = target_s
        asyncio.create_task(_drain_stderr(session))
        asyncio.create_task(_feed_progress_reader(pr, session))
        out = proc.stdout
        spawn_bytes = 0
        while True:
            chunk = await out.read(256 * 1024)
            if not chunk:
                break
            async with session.stdout_lock:
                with session.path.open("ab") as fh:
                    fh.write(chunk)
            if not session.ready.is_set():
                session.ready.set()
            spawn_bytes += len(chunk)
        await proc.wait()
        if session.done:
            break
        if session.duration_s and target_s >= session.duration_s * 0.97:
            session.completed = True
            logger.info("resume download complete (content end reached): %s", session.title[:40])
            break
        expected = (total - session.resume_byte) if total else 0
        if expected and _file_size(session) >= expected * 0.99:
            session.completed = True
            logger.info("resume download complete: %s (%s bytes)", session.title, _file_size(session))
            break
        respawn += 1
        zero_progress = zero_progress + 1 if spawn_bytes == 0 else 0
        logger.warning("resume ffmpeg exited early (respawn #%d) at %s bytes — target %.0fs for %s", respawn, _file_size(session), target_s, session.title[:40])
        if zero_progress >= 3 or respawn >= 6:
            logger.error("resume feed giving up for %s after %d respawns (%d without progress) — marking done", session.title[:40], respawn, zero_progress)
            session.done = True
            break
        await asyncio.sleep(min(20, 5 * respawn) if spawn_bytes == 0 else min(10, 2 * respawn))
    if session.stderr_log and not session.stderr_log.closed:
        session.stderr_log.close()


async def _probe_duration(url: str) -> float:
    try:
        http_src = url.startswith("http")
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ffprobe", "-v", "error",
            *(["-user_agent", PROVIDER_USER_AGENT] if http_src else []),
            "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def _probe_total(url: str) -> int | None:
    if not url.startswith("http"):
        try:
            return Path(url).stat().st_size
        except OSError:
            return None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                async with client.stream("GET", url, headers={"Range": "bytes=0-0", "User-Agent": PROVIDER_USER_AGENT}) as r:
                    await r.aread()
                    cr = r.headers.get("Content-Range", "")
                    if "/" in cr:
                        total = int(cr.rsplit("/", 1)[1])
                        if total > 0:
                            return total
        except Exception:
            logger.warning("total-size probe attempt %d failed", attempt + 1, exc_info=True)
        if attempt < 2:
            await asyncio.sleep(2)
    return None


async def _prime(session: ProxySession) -> None:
    try:
        if session.mode == "hls":
            for _ in range(120):
                if session.path and (session.path / "index.m3u8").exists():
                    segs = len(list(session.path.glob("seg*.ts")))
                    if segs >= 3:
                        break
                await asyncio.sleep(0.5)
        elif session.path is not None:
            threshold = 8 * 1024 * 1024
            for _ in range(180):
                if session.path.exists() and session.path.stat().st_size >= threshold:
                    break
                await asyncio.sleep(0.25)
        elif session.is_live:
            for _ in range(20):
                if session.live_file and session.live_file.exists() and session.live_file.stat().st_size >= 400 * 1024:
                    break
                await asyncio.sleep(0.25)
        else:
            assert session.proc is not None and session.proc.stdout is not None
            async with session.stdout_lock:
                chunk = await session.proc.stdout.read(256 * 1024)
            if chunk:
                session.first_chunk = chunk
    except Exception:
        logger.debug("prime read failed")
    finally:
        session.ready.set()


MAX_LIVE_FILE_BYTES = 4 * 1024**3
MAX_LIVE_SECONDS = 4 * 3600


async def stop_session(token: str) -> None:
    session = _sessions.get(token)
    if session is not None:
        await _kill(session)
    _sessions.pop(token, None)


async def _kill(session: ProxySession) -> None:
    session.done = True
    if session.proc and session.proc.returncode is None:
        try:
            session.proc.kill()
        except ProcessLookupError:
            pass
    if session.mode == "virtual" and session.path and session.path.exists() and session.clients == 0:
        try:
            session.path.unlink(missing_ok=True)
        except OSError:
            pass
    if session.live_file:
        try:
            session.live_file.unlink(missing_ok=True)
        except OSError:
            pass
    if session.proc and session.proc.returncode is None:
        try:
            session.proc.kill()
        except ProcessLookupError:
            pass
    if session.proc is not None and session.proc.returncode not in (0, None):
        try:
            if session.path and (session.path / "ffmpeg.log").exists():
                tail = (session.path / "ffmpeg.log").read_bytes()[-400:]
                logger.warning("ffmpeg (%s) exited %s: %s", session.mode, session.proc.returncode, tail.decode(errors="replace"))
            elif session.path and session.path.is_file():
                logger.warning("ffmpeg (%s) exited %s", session.mode, session.proc.returncode)
        except OSError:
            pass
    session.done = True
    if session.path is not None:
        try:
            if session.path.is_dir():
                shutil.rmtree(session.path, ignore_errors=True)
            else:
                session.path.unlink(missing_ok=True)
        except OSError:
            pass


async def stop_all() -> None:
    async with _lock:
        for session in _sessions.values():
            await _kill(session)
        _sessions.clear()


async def _spawn_ffmpeg(session: ProxySession) -> None:
    if session.mode == "cache":
        if session.start_offset > 0 and session.duration_s > 0:
            session.feed_generation += 1
            asyncio.create_task(_cache_feed_task(session, transcode=False, my_generation=session.feed_generation))
        else:
            asyncio.create_task(_download_loop(session, session.feed_generation))
        return
    if session.mode == "cache_downgrade":
        session.feed_generation += 1
        asyncio.create_task(_cache_feed_task(
            session, transcode=True, my_generation=session.feed_generation,
            map_args=_map_args(session, "mpegts"),
        ))
        return
    if session.mode == "cache_remux":
        session.feed_generation += 1
        asyncio.create_task(_cache_feed_task(
            session, transcode=False, my_generation=session.feed_generation,
            container="matroska", map_args=_map_args(session, "matroska"),
        ))
        return
    if session.mode == "cache_mp4":
        session.feed_generation += 1
        asyncio.create_task(_cache_feed_task(
            session, transcode=False, my_generation=session.feed_generation,
            container="mp4", extra=["-movflags", "+frag_keyframe+empty_moov+default_base_moof"],
            map_args=_map_args(session, "mp4"),
        ))
        return
    transcode = session.mode in ("downgrade", "cache_downgrade")
    if transcode:
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-vf", "scale=w=1920:h=1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"]
        acodec = ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    else:
        vcodec = ["-c:v", "copy"]
        acodec = ["-c:a", "copy"]
    container = "mpegts" if session.mode == "cache_downgrade" else "matroska"

    stderr_target: int | None
    seek_args = ["-ss", f"{session.start_offset:.3f}"] if session.start_offset > 0 else []
    if session.mode == "cache":
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            *seek_args,
            "-i", session.provider_url,
            *vcodec, *acodec,
            "-f", container, str(session.path),
        ]
        stdout = asyncio.subprocess.DEVNULL
        stderr_target = asyncio.subprocess.PIPE
        session.stderr_log = (session.path.parent / f"ffmpeg-{session.token}.log").open("wb")
        cwd = None
    elif session.mode == "hls":
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "warning",
            "-user_agent", PROVIDER_USER_AGENT,
            "-i", session.provider_url,
            *vcodec, *acodec,
            *_map_args(session, "hls"),
            "-f", "hls",
            "-hls_time", "6",
            "-hls_list_size", "0",
            "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_filename", "seg%05d.ts",
            "index.m3u8",
        ]
        stdout = asyncio.subprocess.DEVNULL
        stderr_target = asyncio.subprocess.PIPE
        session.stderr_log = (session.path / "ffmpeg.log").open("wb")
        cwd = str(session.path)
    elif session.path is not None:
        container = "mpegts" if session.mime == "video/mpeg" else "matroska"
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            *seek_args,
            "-user_agent", PROVIDER_USER_AGENT,
            "-i", session.provider_url,
            *vcodec, *acodec,
            *_map_args(session, container),
            "-f", container, str(session.path),
        ]
        stdout = asyncio.subprocess.DEVNULL
        stderr_target = asyncio.subprocess.PIPE
        session.stderr_log = (session.path.parent / f"ffmpeg-{session.token}.log").open("wb")
        cwd = None
    else:
        if not session.is_live:
            if session.duration_s <= 0:
                session.duration_s = await _probe_duration(session.provider_url)
            if session.total is None:
                session.total = await _probe_total(session.provider_url)
            if session.duration_s > 0 and session.total:
                remaining = max(60.0, session.duration_s - session.start_offset)
                session.est_length = int(session.total * remaining / session.duration_s * 1.05)
                logger.info("pipe session est_length=%d (%.0fs remaining of %.0fs) for %s", session.est_length, remaining, session.duration_s, session.title[:40])
        live_args = ["-re", "-stream_loop", "-1", "-fflags", "+genpts"] if session.is_live else []
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            *live_args,
            *seek_args,
            "-user_agent", PROVIDER_USER_AGENT,
            "-i", session.provider_url,
            *vcodec, *acodec,
            "-f", "mpegts", "pipe:1",
        ]
        stdout = asyncio.subprocess.PIPE
        stderr_target = asyncio.subprocess.DEVNULL
        cwd = None
    session.proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=stdout,
        stderr=stderr_target,
        cwd=cwd,
    )
    if stderr_target == asyncio.subprocess.PIPE:
        asyncio.create_task(_drain_stderr(session))


def _rebuild_url_with_fresh_creds(url: str) -> str | None:
    import re as _re
    from .appsettings import provider_config

    match = _re.search(r"/(?P<user>[^/]+)/(?P<pass>[^/]+)/(?P<id>[^/]+\.[a-z0-9]+)$", url)
    if not match:
        return None
    cfg = provider_config()
    if match.group("user") == cfg["xstream_username"] and match.group("pass") == cfg["xstream_password"]:
        return None
    return url[:match.start()] + f"/{cfg['xstream_username']}/{cfg['xstream_password']}/{match.group('id')}"


async def _probe_refusal(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(
            url,
            headers={"Range": "bytes=0-1", "User-Agent": PROVIDER_USER_AGENT},
        )
        body = (response.text or "").strip()
        return f"{response.status_code}: {body[:200]}" if body else f"{response.status_code}: no body"
    except Exception as exc:
        return f"probe failed: {type(exc).__name__}: {exc}"


async def _download_loop(session: ProxySession, my_generation: int = 0) -> None:
    log = session.stderr_log
    backoff = 1.0
    while not session.done and session.feed_generation == my_generation:
        pos = _file_size(session)
        if session.total and session.resume_byte + pos >= session.total:
            session.completed = True
            logger.info("download complete: %s (%s bytes)", session.title, pos)
            from .telemetry import emit
            emit("downloader", "complete", "ok", title=session.title, bytes=pos)
            break
        if asyncio.get_running_loop().time() - session.last_client_seen > 180:
            logger.info("download idle (no TV client for 3 min) — stopping: %s", session.title)
            break
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                async with client.stream(
                    "GET", session.provider_url,
                    headers={"Range": f"bytes={session.resume_byte + pos}-", "User-Agent": PROVIDER_USER_AGENT},
                ) as r:
                    if r.status_code == 200 and pos > 0:
                        logger.warning("server ignored Range (200 at %s) — restarting download from 0 to avoid corruption", pos)
                        from .telemetry import emit
                        emit("downloader", "range_ignored", "restarting", at_bytes=pos)
                        if session.path is not None:
                            with session.path.open("r+b") as fh:
                                fh.truncate(0)
                        continue
                    if r.status_code in (401, 403):
                        fresh = _rebuild_url_with_fresh_creds(session.provider_url)
                        if fresh and fresh != session.provider_url:
                            logger.warning("provider %s — retrying with refreshed credentials", r.status_code)
                            session.provider_url = fresh
                            continue
                    if r.status_code >= 400:
                        reason = await _probe_refusal(client, session.provider_url)
                        logger.warning("provider refused %s: %s", r.status_code, reason)
                        raise httpx.HTTPError(f"provider returned {r.status_code} ({reason})")
                    with session.path.open("ab") as fh:
                        async for chunk in r.aiter_bytes(256 * 1024):
                            if session.done:
                                break
                            fh.write(chunk)
                            backoff = 1.0
        except Exception as exc:
            if session.done:
                break
            got = _file_size(session) - pos
            logger.warning("download interrupted at %s bytes (+%s this round): %s — resuming", pos, got, type(exc).__name__)
            from .telemetry import emit
            emit("downloader", "interrupted", "resuming", at_bytes=pos, gained=got, error=type(exc).__name__)
            if log and not log.closed:
                log.write(f"resume at {pos}: {exc}\n".encode())
                log.flush()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)
    if log and not log.closed:
        log.close()
    session.done = True


async def _drain_stderr(session: ProxySession) -> None:
    assert session.proc is not None and session.proc.stderr is not None
    log = session.stderr_log
    try:
        while True:
            line = await session.proc.stderr.readline()
            if not line:
                break
            if log and not log.closed:
                log.write(line)
                log.flush()
    except Exception:
        pass
    finally:
        if log and not log.closed:
            log.close()


FAKE_LENGTH = 2**31 - 1
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _file_size(session: ProxySession) -> int:
    try:
        return session.path.stat().st_size if session.path else 0
    except OSError:
        return 0


async def _serve_cache(request, session: ProxySession):
    if session.deferred:
        async with session.deferred_lock:
            if session.deferred:
                session.deferred = False
                await _spawn_ffmpeg(session)
                asyncio.create_task(_prime(session))
                logger.info("deferred session %s started on first fetch", session.token)
    if session.mode != "virtual":
        try:
            await asyncio.wait_for(session.ready.wait(), timeout=50)
        except asyncio.TimeoutError:
            pass
    total = _file_size(session)
    for _ in range(20):
        if session.path is not None and session.path.exists() and session.path.stat().st_size > 0:
            total = _file_size(session)
            break
        await asyncio.sleep(0.25)
    range_header = request.headers.get("Range", "")
    match = RANGE_RE.match(range_header.strip()) if range_header else None
    start = int(match.group(1)) if match and match.group(1) else 0
    if session.mode == "virtual":
        logger.info("virtual %s %s: Range=%r declared=%s filesize=%s", request.method, session.token, range_header, session.total, _file_size(session))
    if match and not match.group(1) and match.group(2):
        start = max(0, total - int(match.group(2)))

    if session.total is None:
        session.total = await _probe_total(session.provider_url)
        if session.total is not None:
            logger.info("late total-size probe recovered: %s bytes for %s", session.total, session.title)
    declared_total = (session.total - session.resume_byte) if session.total else total
    if session.declare_current:
        declared_total = max(1, _file_size(session))
    if start >= declared_total:
        if session.declare_current and not session.completed and not session.done:
            edge_deadline = asyncio.get_running_loop().time() + 30
            while start >= _file_size(session) and not session.completed and not session.done:
                if asyncio.get_running_loop().time() > edge_deadline:
                    break
                await asyncio.sleep(0.25)
            declared_total = max(1, _file_size(session))
        if start >= declared_total:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{declared_total}"})
    if (not session.declare_current) and session.mode in ("virtual", "cache_downgrade") and start >= _file_size(session):
        sliver = b""
        try:
            with session.path.open("rb") as fh:
                fh.seek(max(0, _file_size(session) - 275))
                sliver = fh.read(275)
        except OSError:
            pass
        end = min(start + max(1, len(sliver)), declared_total) - 1
        return web.Response(
            status=206,
            headers={
                "Content-Type": session.mime,
                "Content-Range": f"bytes {start}-{end}/{declared_total}",
                "Content-Length": str(len(sliver)),
                "Accept-Ranges": "bytes",
            },
            body=sliver,
        )
    deadline = asyncio.get_running_loop().time() + 90
    while start >= total and start > 0:
        if session.proc and session.proc.returncode is not None:
            break
        if asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.3)
        total = _file_size(session)

    if session.mode == "virtual":
        logger.info("virtual serve enter: method=%s start=%d declared=%s filesize=%d", request.method, start, declared_total, _file_size(session))
    if request.method == "HEAD":
        head_headers = {
            "Content-Type": session.mime,
            "Accept-Ranges": "bytes",
            "Content-Length": str(declared_total),
        }
        return web.Response(status=200, headers=head_headers)

    headers = {
        "Content-Type": session.mime,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    status = 200
    stop_offset = None
    if match:
        requested_end = int(match.group(2)) if match.group(2) else None
        if requested_end is not None:
            stop_offset = min(requested_end + 1, declared_total)
        status = 206
        end = (stop_offset - 1) if stop_offset is not None else declared_total - 1
        headers["Content-Range"] = f"bytes {start}-{end}/{declared_total}"
    if session.declare_current and stop_offset is None:
        stop_offset = declared_total
    length = (stop_offset - start) if stop_offset is not None else (declared_total - start)
    headers["Content-Length"] = str(length)

    frontier_deadline = asyncio.get_running_loop().time() + 300
    while start >= total and start > 0 and session.total:
        if session.proc and session.proc.returncode is not None:
            break
        if asyncio.get_running_loop().time() > frontier_deadline:
            break
        await asyncio.sleep(0.3)
        total = _file_size(session)
    if start >= total:
        return web.Response(status=416)

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)
    session.clients += 1
    offset = start
    try:
        with session.path.open("rb") as fh:
            while True:
                limit = stop_offset if stop_offset is not None else _file_size(session)
                if offset < limit:
                    fh.seek(offset)
                    chunk = fh.read(min(256 * 1024, limit - offset))
                    if chunk:
                        try:
                            await resp.write(chunk)
                        except (ConnectionError, ConnectionResetError):
                            logger.debug("client disconnected mid-stream (%s)", session.token)
                            return resp
                        offset += len(chunk)
                        session.max_served = max(session.max_served, offset)
                        continue
                if stop_offset is not None and offset >= stop_offset:
                    break
                proc_alive = session.proc is not None and session.proc.returncode is None
                if not proc_alive and offset >= _file_size(session):
                    break
                if offset >= _file_size(session) and session.total:
                    if asyncio.get_running_loop().time() > frontier_deadline:
                        break
                await asyncio.sleep(0.15)
    finally:
        session.clients = max(0, session.clients - 1)
        resp.force_close()
    return resp


_LAN_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
]


def _client_allowed(request) -> bool:
    peer = request.remote or ""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(ip in net for net in _LAN_NETS)


async def stream_handler(request):
    if not _client_allowed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    token = request.match_info["token"]
    logger.info("handler enter: %s %s (t=%.3f)", request.method, token, asyncio.get_running_loop().time())
    session = _sessions.get(token)
    if session is None or session.done:
        return web.json_response({"error": "unknown session"}, status=404)
    session.last_client_seen = asyncio.get_running_loop().time()
    return await _dispatch_stream(request, session)


async def signed_stream_handler(request):
    from . import urlsign

    if not _client_allowed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    token = request.match_info["token"]
    exp = request.query.get("exp", "")
    sig = request.query.get("sig", "")
    if not urlsign.verify(token, exp, sig):
        return web.json_response({"error": "bad or expired signature"}, status=403)
    session = _sessions.get(token)
    if session is None or session.done:
        return web.json_response({"error": "unknown session"}, status=404)
    session.last_client_seen = asyncio.get_running_loop().time()
    return await _dispatch_stream(request, session)


async def _dispatch_stream(request, session):
    if session.mode == "hls":
        playlist = session.path / "index.m3u8"
        body = b""
        for _ in range(10):
            if playlist.exists():
                body = playlist.read_bytes()
                if body.strip():
                    break
            await asyncio.sleep(0.25)
        if not body.strip():
            return web.Response(status=503, text="playlist not ready")
        return web.Response(body=body, headers={
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "no-store",
        })
    if session.path is not None:
        return await _serve_cache(request, session)
    headers = {
        "Content-Type": session.mime,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    if request.method == "HEAD":
        response = web.StreamResponse(status=200, headers=headers)
        if session.is_live:
            headers["Accept-Ranges"] = "none"
        if session.est_length:
            response.content_length = session.est_length
        elif session.is_live:
            response.content_length = FAKE_LENGTH
        await response.prepare(request)
        await response.write_eof()
        return response
    async with _lock:
        if session.proc is None:
            await _spawn_ffmpeg(session)

    if not session.is_live and not session.ready.is_set():
        try:
            await asyncio.wait_for(session.ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass

    if session.is_live:
        headers["Accept-Ranges"] = "none"
        rng = ""
    rng = request.headers.get("Range", "")
    start = 0
    if rng.startswith("bytes="):
        try:
            start = int(rng.split("=", 1)[1].split("-", 1)[0] or 0)
        except ValueError:
            start = 0
    resp = web.StreamResponse(status=206 if start else 200, headers=headers)
    if session.is_live:
        pass
    elif session.est_length and session.est_length > start:
        if start:
            resp.headers["Content-Range"] = f"bytes {start}-{session.est_length - 1}/{session.est_length}"
        resp.content_length = session.est_length - start
    else:
        resp.force_close()
    await resp.prepare(request)
    session.clients += 1
    try:
        if session.is_live and session.live_file:
            offset = 0
            stall = 0.0
            while not session.done and session.clients > 0:
                size = session.live_file.stat().st_size if session.live_file.exists() else 0
                if size > offset:
                    with session.live_file.open("rb") as fh:
                        fh.seek(offset)
                        data = fh.read(min(size - offset, 512 * 1024))
                    if data:
                        await resp.write(data)
                        offset += len(data)
                        session.last_client_seen = asyncio.get_running_loop().time()
                        stall = 0.0
                else:
                    await asyncio.sleep(0.2)
                    stall += 0.2
                    if stall > 45:
                        logger.warning("live file stopped growing for %s — ending stream", session.token)
                        break
        else:
            offset = 0
            if session.first_chunk:
                await resp.write(session.first_chunk)
                offset += len(session.first_chunk)
                session.first_chunk = None
            assert session.proc is not None and session.proc.stdout is not None
            while True:
                async with session.stdout_lock:
                    chunk = await session.proc.stdout.read(64 * 1024)
                offset += len(chunk) if chunk else 0
                session.max_served = max(session.max_served, offset)
                session.last_client_seen = asyncio.get_running_loop().time()
                if not chunk:
                    break
                await resp.write(chunk)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        session.clients = max(0, session.clients - 1)
        if session.clients <= 0 and not session.is_live:
            if session.proc and session.proc.returncode is None:
                try:
                    session.proc.kill()
                except ProcessLookupError:
                    pass
            session.done = True
    return resp


SEG_NAME_RE = re.compile(r"^(seg\d{5}\.ts|index\.m3u8)$")


async def hls_segment_handler(request):
    if not _client_allowed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    token = request.match_info["token"]
    name = request.match_info["name"]
    session = _sessions.get(token)
    if session is not None:
        session.last_client_seen = asyncio.get_running_loop().time()
    if session is None or session.mode != "hls" or not SEG_NAME_RE.match(name):
        return web.json_response({"error": "not found"}, status=404)
    seg = session.path / name
    if not seg.exists():
        return web.Response(status=503, text="segment not ready")
    mime = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
    return web.FileResponse(seg, headers={"Content-Type": mime, "Cache-Control": "no-store"})


async def subs_vtt(request):
    from . import library
    from .osubs import srt_to_vtt
    from aiohttp import web as _web

    media_key = request.match_info["key"]
    try:
        data = await asyncio.to_thread(library.get_subs_for_stream, media_key)
    except Exception:
        logger.exception("subs extract failed for %s", media_key)
        data = None
    if not data:
        return _web.Response(status=404, text="no subs")
    return _web.Response(body=srt_to_vtt(data), content_type="text/vtt", charset="utf-8")

async def library_file(request):
    if not _client_allowed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    rel = request.match_info["path"]
    target = (LIBRARY_DIR / rel).resolve()
    if not str(target).startswith(str(LIBRARY_DIR.resolve())) or not target.is_file():
        return web.json_response({"error": "not found"}, status=404)
    mime = "video/x-matroska" if target.suffix == ".mkv" else "video/mp4"
    return web.FileResponse(target, headers={"Accept-Ranges": "bytes", "Content-Type": mime})


async def start_server(port: int = PROXY_PORT) -> None:
    if CACHE_DIR.exists():
        for orphan in CACHE_DIR.iterdir():
            try:
                if orphan.is_dir():
                    shutil.rmtree(orphan, ignore_errors=True)
                else:
                    orphan.unlink()
            except OSError:
                pass
    app = web.Application()
    app.router.add_get("/stream/{token}", stream_handler)
    app.router.add_get("/s/{token}.{ext}", signed_stream_handler)
    app.router.add_get("/s/{token:[a-f0-9]+}", signed_stream_handler)
    app.router.add_get("/stream/{token}/{name}", hls_segment_handler)
    app.router.add_get("/library/{path:.*}", library_file)
    app.router.add_get(r"/subs/{key:[\\w:]+}\\.vtt", subs_vtt)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def check_url_reachable(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.head(url, follow_redirects=True, headers={"User-Agent": PROVIDER_USER_AGENT})
            return response.status_code < 400
    except httpx.HTTPError:
        return False
