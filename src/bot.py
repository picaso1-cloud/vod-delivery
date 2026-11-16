import asyncio
import json
import logging
import re
import uuid
import sys
import time
from pathlib import Path
from dataclasses import dataclass

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from . import auditor, fastpath, library, provider, proxy, store, telemetry
from .appsettings import is_allowed
from .config import BOT_TOKEN
from .llm import parse_request, suggest as llm_suggest
from .provider import Provider
from .provider import health as provider_health


from .parsing import normalize_for_grouping
from .search import Group, keyword_search, search as search_media
from .ssdp import Renderer, discover_renderers
from .caster import cast, get_position, playback_state, probe_duration, seek, stop

logger = logging.getLogger("vod-delivery")
_DISCOVER_CACHE: dict[str, tuple[float, dict]] = {}
_DISCOVER_AI_CACHE: dict[str, tuple[float, dict]] = {}
_STARTED_AT = time.time()


async def _resolve_media_key(media_key: str) -> dict | None:
    kind, _, ref = media_key.partition(":")
    if kind == "movie" and not ref.isdigit():
        return None
    if kind == "episode":
        ep_part, _, series_part = ref.partition(":")
        if not ep_part.isdigit() or (series_part and not series_part.isdigit()):
            return None
    if kind not in ("movie", "episode"):
        return None
    payload = None
    conn = store.connect()
    if kind == "movie":
        row = conn.execute("SELECT * FROM movies WHERE stream_id=?", (int(ref),)).fetchone()
        if row:
            payload = {
                "kind": "movie", "ref_id": row["stream_id"], "media_key": media_key,
                "title": f"{row['title']} ({row['year'] or ''})".strip(), "lang": row["lang"],
                "url": provider.movie_url(row["stream_id"], row["ext"]), "ext": row["ext"],
            }
    elif kind == "episode":
        ep_id, _, series_ref = ref.partition(":")
        row = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (int(ep_id),)).fetchone()
        if row is None and series_ref:
            info = await provider.series_info(int(series_ref))
            for season_key, eplist in info.get("episodes", {}).items():
                for e in eplist:
                    conn.execute(
                        "INSERT OR REPLACE INTO episodes(episode_id, series_id, season, episode, title, ext) VALUES(?,?,?,?,?,?)",
                        (int(e["id"]), int(series_ref), int(season_key), int(e.get("episode_num", 0)),
                         (e.get("title") or "").strip(), e.get("container_extension", "mkv")),
                    )
                conn.commit()
            row = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (int(ep_id),)).fetchone()
        if row:
            srow = conn.execute("SELECT title FROM series WHERE series_id=?", (row["series_id"],)).fetchone()
            series_name = srow["title"] if srow else None
            label = f"S{row['season']}E{row['episode']}"
            payload = {
                "kind": "episode", "ref_id": row["episode_id"], "media_key": media_key,
                "title": f"{series_name} — {label}" if series_name else label, "lang": None,
                "url": provider.episode_url(row["episode_id"], row["ext"]), "ext": row["ext"],
            }
    conn.close()
    return payload

def _protocol_info_for(tv_name: str, ext: str) -> str | None:
    try:
        conn = store.connect()
        row = conn.execute("SELECT udn FROM tvs WHERE name=?", (tv_name,)).fetchone()
        digest = store.get_tv_sink_protocols(conn, row["udn"]) if row else None
        conn.close()
    except Exception:
        return None
    if not digest:
        return None
    from .capabilities import best_delivery_mime, choose_protocol_info
    from .caster import MIME_BY_EXT
    mime = best_delivery_mime(ext, digest, MIME_BY_EXT.get(ext, "video/mpeg"))
    return choose_protocol_info(mime, digest)

MAX_CANDIDATES = 5
MAX_EPISODE_BUTTONS = 48
POLL_INTERVAL = 30
POLL_MAX_SECONDS = 4 * 3600

provider = Provider()
sessions: dict[int, dict] = {}


@dataclass
class ActivePlay:
    play_id: int
    renderer: Renderer
    tv_name: str
    media_key: str
    task: asyncio.Task | None = None
    chat_id: int | None = None
    message_id: int | None = None
    title: str = ""
    paused: bool = False
    muted: bool = False
    proxy_token: str | None = None
    seeking: bool = False


_SEEK_STEPS = [10, 30, 60, 180, 300, 600]
_seek_state: dict[int, dict] = {}

@dataclass
class CastTarget:
    friendly_name: str
    udn: str
    ip: str


active_play: ActivePlay | None = None


def _session(chat_id: int) -> dict:
    if chat_id not in sessions:
        sessions[chat_id] = {
            "renderers": [],
            "selected": None,
            "groups": [],
            "pending": None,
            "resume_pos": None,
        }
    return sessions[chat_id]



def _lang_label(v) -> str:
    name = {"EN": "EN", "FR": "FR", "AR": "AR", None: "ORIG"}.get(v.lang, v.lang)
    if v.subs_lang:
        return f"{name}+{v.subs_lang}sub"
    return name


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


_CONTINUE_RE = re.compile(r"\b(continue|resume|left\s?off|left\s?of|where\s+i\s+(was|stopped|left)|pick\s?up)\b", re.IGNORECASE)
_NEXT_RE = re.compile(r"\bnext\b|\bskip\b|التال[ية]|suivant", re.IGNORECASE)


def _title_words(title: str | None) -> set[str]:
    return {w for w in re.split(r"\W+", (title or "").lower()) if len(w) > 3}


def _tv_keyboard(session: dict) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("▶️ " if i == session.get("selected") else "📺 ") + r.friendly_name,
                callback_data=f"tv:{i}",
            )
        ]
        for i, r in enumerate(session["renderers"])
    ]
    if session.get("pending"):
        rows.append([InlineKeyboardButton(text="📱 Link (phone / remote)", callback_data="link:0")])
    rows.append([InlineKeyboardButton(text="🔄 Rescan", callback_data="rescan")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _group_keyboard(gi: int, group: Group) -> InlineKeyboardMarkup:
    if group.kind == "movie":
        rows = [[InlineKeyboardButton(text="▶ " + _lang_label(v), callback_data=f"sel:{gi}:{vi}")] for vi, v in enumerate(group.variants[:8])]
        rows.append([InlineKeyboardButton(text="➕ Watchlist", callback_data=f"wl:{gi}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Browse episodes", callback_data=f"sel:{gi}:0")],
            [InlineKeyboardButton(text="➕ Watchlist", callback_data=f"wl:{gi}")],
        ]
    )


async def _send_group(send, gi: int, group: Group, note: str) -> None:
    langs = " · ".join(_lang_label(v) for v in group.variants[:6])
    caption = f"<b>{group.title}</b>" + (f" ({group.year})" if group.year else "") + f"\n<i>{langs}</i>\n{note}"
    keyboard = _group_keyboard(gi, group)
    if group.poster:
        try:
            await send.answer_photo(group.poster, caption=caption, reply_markup=keyboard)
            return
        except Exception:
            logger.warning("poster send failed for %s; falling back to text", group.title)
    await send.answer(caption, reply_markup=keyboard)


async def _persist_renderers(renderers: list[Renderer]) -> list[Renderer]:
    conn = store.connect()
    try:
        merged = []
        for r in renderers:
            ip = r.location.split("//")[-1].split(":")[0] if r.location else None
            mac = await asyncio.to_thread(store.arp_mac, ip) if ip else None
            if mac and store.is_banned_mac(conn, mac):
                conn.execute("UPDATE tvs SET ignored=1 WHERE mac=? OR udn=?", (mac, r.udn))
                conn.commit()
                continue
            store.upsert_tv(conn, r.udn, r.friendly_name, ip, r.location, mac=mac)
            ignored_row = None
            known = store.get_tv(conn, r.udn)
            if known and known["ignored"]:
                ignored_row = known
            if mac and ignored_row is None:
                by_mac = store.find_tv_by_mac(conn, mac)
                if by_mac and by_mac["ignored"]:
                    ignored_row = by_mac
            if ignored_row is not None:
                conn.execute("UPDATE tvs SET ignored=1 WHERE udn=?", (r.udn,))
                conn.commit()
                continue
            manual = store.find_tv_by_mac(conn, mac) if mac else None
            if manual and manual["source"] == "manual":
                store.set_tv_flags(
                    conn, r.udn,
                    wol_enabled=manual["wol_enabled"],
                    prefer_proxy=manual["prefer_proxy"],
                    downgrade=manual["downgrade"],
                    mac=mac,
                )
                conn.execute("UPDATE tvs SET name=?, source='renamed' WHERE udn=?", (manual["name"], r.udn))
                conn.commit()
                known = store.get_tv(conn, r.udn)
            name = known["name"] if known and known["source"] == "renamed" else r.friendly_name
            merged.append(Renderer(location=r.location, friendly_name=name, udn=r.udn))
        return merged
    finally:
        conn.close()


async def _ip_for_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    import subprocess as _sp

    try:
        out = await asyncio.to_thread(
            lambda: _sp.run(["ip", "-4", "neigh"], capture_output=True, text=True, timeout=5).stdout
        )
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and store.arp_mac(parts[0]) and store.arp_mac(parts[0]).lower() == mac.lower():
            return parts[0]
    return None


async def _discover_merged() -> list:
    dlna = await _persist_renderers(await discover_renderers())
    conn = store.connect()
    cast_rows = conn.execute("SELECT * FROM tvs WHERE kind='cast' AND ignored=0 AND ip IS NOT NULL").fetchall()
    dlna_rows = conn.execute("SELECT * FROM tvs WHERE kind='dlna' AND ignored=0 AND location IS NOT NULL").fetchall()
    conn.close()

    seen = {r.udn for r in dlna}
    result = list(dlna)

    async def port_alive(ip: str, port: int, timeout: float = 1.5) -> bool:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
            w.close()
            return True
        except Exception:
            return False

    async def dlna_alive(location: str) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(location, follow_redirects=True)
                return resp.status_code == 200 and "<device" in resp.text
        except Exception:
            return False

    for r in cast_rows:
        if r["udn"] in seen:
            continue
        if await port_alive(r["ip"], 8009):
            result.append(CastTarget(friendly_name=r["name"], udn=r["udn"], ip=r["ip"]))
        seen.add(r["udn"])

    for t in dlna_rows:
        if t["udn"] in seen:
            continue
        if await dlna_alive(t["location"]):
            result.append(Renderer(location=t["location"], friendly_name=t["name"], udn=t["udn"]))
        seen.add(t["udn"])
    return result


async def _ensure_tv(send, chat_id: int) -> bool:
    session = _session(chat_id)
    if session.get("selected") is not None:
        return True
    await send.answer("📺 Looking for TVs…")
    renderers = await _discover_merged()
    session["renderers"] = renderers
    session["selected"] = None
    if not renderers:
        session["pending"] = None
        await send.answer("No DLNA TVs found. Power them on (standby usually works) and tap the language again.")
        return False
    await send.answer("Where do we watch?", reply_markup=_tv_keyboard(session))
    return False


async def _stop_active(reason: str = "new cast") -> str:
    global active_play
    if active_play is None:
        return ""
    info = f"⏹ Stopped previous playback on <b>{active_play.tv_name}</b> ({reason}).\n"
    target = active_play.renderer
    try:
        from .cc import stop as cc_stop

        if isinstance(target, CastTarget):
            cc_stop(target.ip)
        else:
            await stop(target)
    except Exception:
        pass
    if active_play.task:
        active_play.task.cancel()
    active_play = None
    await proxy.stop_all()
    return info


async def _poll_playback_dlna(play_id: int, renderer: Renderer, user_id: int, media_key: str, proxy_token: str | None = None) -> None:
    conn = store.connect()
    elapsed = 0
    last_pos = -1.0
    stalls = 0
    nudges = 0
    last_nudge = 0.0
    gave_up_note = False
    try:
        while elapsed < POLL_MAX_SECONDS:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            try:
                state = await playback_state(renderer)
                pos, dur = await get_position(renderer)
                if proxy_token:
                    s = proxy.get_session(proxy_token)
                    merged = proxy.progress_seconds(s) if s else None
                    if merged:
                        ppos, pdur = merged
                        if ppos > pos:
                            pos = ppos
                        if dur <= 0 or dur > 24 * 3600:
                            dur = pdur
                if pos > 0 or state == "PLAYING":
                    store.update_play_position(conn, play_id, pos, dur, completed=dur > 0 and pos >= dur * 0.95)
                    store.set_progress(conn, user_id, media_key, pos, dur)
                if state == "PLAYING" and dur > 0 and abs(pos - last_pos) < 1.0:
                    stalls += 1
                    if stalls >= 3 and time.time() - last_nudge > 30:
                        if nudges < 3:
                            nudges += 1
                            last_nudge = time.time()
                            stalls = 0
                            logger.warning("DLNA playback stalled at %.0fs — nudge seek #%d", pos, nudges)
                            telemetry.emit("playback", "stall_nudge", "attempted", pos=round(pos), nudge=nudges)
                            if not await seek(renderer, pos + 5):
                                logger.warning("stall nudge seek rejected")
                        elif not gave_up_note:
                            gave_up_note = True
                            logger.error("DLNA playback stalled after %d nudges at %.0fs — manual re-cast needed", nudges, pos)
                            if _bot:
                                await _notify_owner(
                                    f"🥶 {renderer.friendly_name} stalled at {int(pos)}s and seek nudges failed — re-cast from continue list.")
                else:
                    stalls = 0
                last_pos = pos
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("poll cycle failed")
        if state not in ("PLAYING", "TRANSITIONING"):
            try:
                if active_play and active_play.play_id == play_id:
                    ap = active_play
                    active_play = None
                    telemetry.emit("playback", "ended", "ok", title=ap.title, tv=ap.tv_name)
                    if ap.chat_id and ap.message_id and _bot:
                        await _bot.edit_message_text(
                            f"⏹ <b>{ap.title}</b> — playback ended", chat_id=ap.chat_id, message_id=ap.message_id,
                            reply_markup=_ended_keyboard(ap.media_key),
                        )
            except Exception:
                logger.exception("end-of-play controls update failed")
            return
        try:
            await _refresh_controls()
        except Exception:
            pass
    finally:
        conn.close()


async def _poll_playback_cast(play_id: int, target: CastTarget, user_id: int, media_key: str, proxy_token: str | None = None) -> None:
    import time as _time

    from .cc import media_status

    conn = store.connect()
    start = _time.time()
    try:
        last_pos = -1.0
        stalls = 0
        nudges = 0
        last_nudge = 0.0
        while _time.time() - start < POLL_MAX_SECONDS:
            await asyncio.sleep(POLL_INTERVAL)
            ms = media_status(target.ip)
            if ms is None:
                return
            state, pos, dur = ms
            if proxy_token:
                s = proxy.get_session(proxy_token)
                merged = proxy.progress_seconds(s) if s else None
                if merged:
                    ppos, pdur = merged
                    if ppos > pos:
                        pos = ppos
                    if dur <= 0 or dur > 24 * 3600:
                        dur = pdur
            store.update_play_position(conn, play_id, pos, dur)
            if media_key:
                store.set_progress(conn, user_id, media_key, pos, dur)
            if state == "PLAYING" and dur > 0 and abs(pos - last_pos) < 1.0:
                stalls += 1
                if stalls >= 3 and _time.time() - last_nudge > 30 and nudges < 3:
                    from .cc import seek as cc_seek
                    nudges += 1
                    last_nudge = _time.time()
                    stalls = 0
                    logger.warning("cast playback stalled at %.0fs — nudge seek #%d", pos, nudges)
                    cc_seek(target.ip, pos + 5)
            else:
                stalls = 0
            last_pos = pos
            if state not in ("PLAYING", "BUFFERING"):
                try:
                    if active_play and active_play.play_id == play_id:
                        ap = active_play
                        active_play = None
                        telemetry.emit("playback", "ended", "ok", title=ap.title, tv=ap.tv_name)
                        if ap.chat_id and ap.message_id and _bot:
                            await _bot.edit_message_text(
                                f"⏹ <b>{ap.title}</b> — playback ended", chat_id=ap.chat_id, message_id=ap.message_id,
                                reply_markup=_ended_keyboard(ap.media_key),
                            )
                except Exception:
                    logger.exception("end-of-play controls update failed")
                return
            try:
                await _refresh_controls()
            except Exception:
                pass
    finally:
        conn.close()


async def _execute_cast(session: dict, chat_id: int, user_id: int) -> str:
    global active_play
    previous = _CAST_TASKS.get(chat_id)
    if previous and not previous.done():
        previous.cancel()
        logger.info("cancelled in-flight cast for chat %s — new pick replaces it", chat_id)
    _CAST_TASKS[chat_id] = asyncio.current_task()
    try:
        return await _execute_cast_inner(session, chat_id, user_id)
    except asyncio.CancelledError:
        logger.info("cast superseded for chat %s", chat_id)
        return "↪️ Superseded by a newer pick."
    finally:
        if _CAST_TASKS.get(chat_id) is asyncio.current_task():
            _CAST_TASKS.pop(chat_id, None)


_CAST_TASKS: dict[int, asyncio.Task] = {}
_URL_DL: dict[str, dict] = {}
_LIVE_NAMES: dict[int, str] = {}
_subs_backfill_running = False
_url_dl_active = False


async def _execute_cast_inner(session: dict, chat_id: int, user_id: int) -> str:
    global active_play
    payload = session["pending"]
    proxy_token = None
    if provider_health()["down"]:
        _conn = store.connect()
        _lib = store.get_library_item(_conn, payload["media_key"])
        _conn.close()
        if not (_lib and _lib["path"] and Path(_lib["path"]).exists()):
            return f"🛑 Provider is offline and \u201c{payload['title']}\u201d isn't in your library yet. Downloaded items still play."
    target = session["renderers"][session["selected"]]
    pre = await _stop_active("new cast started")
    if payload.get("live") and provider_health()["down"]:
        return "🛑 Provider is offline — live TV needs the provider connection."

    if isinstance(target, CastTarget):
        from .cc import play as cc_play

        conn = store.connect()
        tv_row = store.get_tv(conn, target.udn)
        conn.close()
        prefer_proxy = bool(tv_row and tv_row["prefer_proxy"])

        if payload.get("live"):
            used = "chromecast+live-hls"
            logger.info("live cast (cast TV): channel %s via HLS direct", payload.get("ref_id"))
            try:
                _cc_start(payload["url"], "application/x-mpegurl")
            except Exception:
                logger.warning("live HLS cast failed — falling back to TS pipe", exc_info=True)
                try:
                    session_obj = await proxy.start_session(payload["url"], payload["title"], mode="copy", mime="video/mpeg", live=True)
                    _cc_start(proxy.proxied_url(session_obj.token, target.ip), "video/mp2t")
                    used = "chromecast+live-ts"
                except Exception:
                    logger.exception("live TS pipe failed")
                    return f"{pre}❌ Live cast failed — the channel may be offline."
            result = f"{pre}📺 <b>{target.friendly_name}</b> → “{payload['title']}”\nPlayer state: <b>PLAYING</b>\n🎛 mode: {used}"
            conn = store.connect()
            play_id = store.start_play(conn, user_id, "live", payload["ref_id"], None, payload["title"], None, target.friendly_name)
            conn.close()
            live_token = None
            if used == "chromecast+live-ts":
                live_token = session_obj.token

            async def _live_cast_poll() -> None:
                try:
                    await _poll_playback_cast(play_id, target, user_id, None)
                finally:
                    if live_token:
                        try:
                            await proxy.stop_session(live_token)
                        except Exception:
                            pass

            task = asyncio.create_task(_live_cast_poll())
            active_play = ActivePlay(play_id=play_id, renderer=target, tv_name=target.friendly_name, media_key=None, task=task, proxy_token=live_token)
            session["last_tv_udn"] = target.udn
            await _send_controls(chat_id, payload["title"], target.friendly_name)
            return result

        mime = payload.get("mime", "video/x-matroska")
        used = "chromecast"
        cast_url = payload["url"]

        conn = store.connect()
        lib = store.get_library_item(conn, payload["media_key"])
        conn.close()
        lib_subs_lang = lib["subs_lang"] if lib else None
        subs_url = ""
        if lib_subs_lang and lib_subs_lang != "none":
            subs_url = f"http://{proxy.lan_ip_for(target.ip)}:{proxy.PROXY_PORT}/subs/{payload['media_key']}.vtt"

        def _cc_start(url: str, m: str) -> None:
            cc_play(target.ip, url, m, payload["title"], subs_url)
        if lib and lib["status"] == "ready" and lib["path"]:
            lib_path = Path(lib["path"])
            if lib_path.exists():
                lib_url = f"http://{proxy.lan_ip_for(target.ip)}:{proxy.PROXY_PORT}/library/{lib_path.relative_to(library.LIBRARY_DIR)}"
                try:
                    _cc_start(lib_url, mime)
                    used = "chromecast+library"
                    cast_url = lib_url
                except Exception:
                    logger.warning("library cast failed — falling back", exc_info=True)

        if prefer_proxy:
            try:
                session_obj = await proxy.start_session(
                    payload["url"], payload["title"], mode="cache", mime=mime,
                    start_offset=session.get("resume_pos") or 0.0,
                )
                cast_url = proxy.proxied_url(session_obj.token, target.ip)
                proxy_token = session_obj.token
                used = "chromecast+file"
                try:
                    await asyncio.wait_for(session_obj.ready.wait(), timeout=12)
                except asyncio.TimeoutError:
                    pass
                try:
                    _cc_start(cast_url, mime)
                except Exception:
                    logger.warning("chromecast file cast failed — falling back direct", exc_info=True)
                    used = "chromecast"
                    cast_url = payload["url"]
                    _cc_start(cast_url, "video/mp4")
            except Exception:
                logger.exception("file session failed — casting direct")
                used = "chromecast"
                cast_url = payload["url"]
                _cc_start(cast_url, "video/mp4")
        else:
            try:
                _cc_start(cast_url, "video/mp4")
            except Exception as exc:
                logger.exception("cast failed")
                return f"{pre}❌ Cast failed: <code>{type(exc).__name__}: {exc}</code>{_provider_down_note()}"

        result = f"{pre}📺 <b>{target.friendly_name}</b> → “{payload['title']}”\nPlayer state: <b>PLAYING</b>\n🎛 mode: {used}"
        conn = store.connect()
        play_id = store.start_play(conn, user_id, payload["kind"], payload["ref_id"], payload["media_key"], payload["title"], payload.get("lang"), target.friendly_name)
        conn.close()
        task = asyncio.create_task(_poll_playback_cast(play_id, target, user_id, payload["media_key"], proxy_token))
        active_play = ActivePlay(play_id=play_id, renderer=target, tv_name=target.friendly_name, media_key=payload["media_key"], task=task, proxy_token=proxy_token)
        session["last_tv_udn"] = target.udn
        note = library.subs_status(payload["media_key"])
        await _send_controls(chat_id, payload["title"], target.friendly_name, note)
        return result

    renderer = target
    conn = store.connect()
    tv_row = store.get_tv(conn, renderer.udn)
    conn.close()
    prefer_proxy = bool(tv_row and tv_row["prefer_proxy"])
    downgrade = bool(tv_row and tv_row["downgrade"])
    try:
        virtual_channel = bool(tv_row and tv_row["virtual_channel"])
    except (KeyError, IndexError):
        virtual_channel = False
    tv_ip = renderer.location.split("//")[-1].split(":")[0]

    if payload.get("live"):
        access_denied = False
        try:
            await stop(renderer)
            await asyncio.sleep(1)
        except Exception:
            pass
        state = "STOPPED"
        used = "live/ts-pipe"
        try:
            session_obj = await proxy.start_session(payload["url"], payload["title"], mode="copy", mime="video/mpeg", live=True)
        except RuntimeError as exc:
            if "live source unavailable" in str(exc):
                logger.info("live cast aborted: channel %s refused by provider", payload.get("ref_id"))
                telemetry.emit("live", "cast", "refused", channel=payload.get("ref_id"))
                return f"{pre}📡 This channel isn't available right now (offline or not in your subscription). Try another one."
            raise
        try:
            state = await cast(renderer, proxy.proxied_url(session_obj.token, tv_ip), payload["title"], "ts", protocol_info=_protocol_info_for(renderer.friendly_name, "ts"))
            waits = 0
            while state == "TRANSITIONING" and waits < 6:
                await asyncio.sleep(5)
                waits += 1
                state = await playback_state(renderer)
        except Exception as exc:
            access_denied = "705" in str(exc)
            logger.warning("live TS pipe failed (%s) — direct HLS attempt", exc)
            try:
                state = await cast(renderer, payload["url"], payload["title"], "ts", protocol_info=_protocol_info_for(renderer.friendly_name, "ts"))
                waits = 0
                while state == "TRANSITIONING" and waits < 6:
                    await asyncio.sleep(5)
                    waits += 1
                    state = await playback_state(renderer)
                used = "live/hls-direct"
            except Exception as exc2:
                access_denied = access_denied or "705" in str(exc2)
                logger.warning("live HLS direct failed: %s", exc2)
                state = "STOPPED"
        result = f"{pre}📺 <b>{renderer.friendly_name}</b> → “{payload['title']}”\nPlayer state: <b>{state}</b>"
        if state != "PLAYING":
            if access_denied:
                return result + (
                    "\n\n🛑 The TV's player refused the stream (access denied — its DLNA stack is stuck, "
                    "usually from an earlier dead stream). Power-cycle the TV (unplug 30 s), then try again."
                )
            return result + "\n\n⚠️ Live cast failed — the provider may be busy or the channel offline."
        conn = store.connect()
        play_id = store.start_play(conn, user_id, "live", payload["ref_id"], None, payload["title"], None, renderer.friendly_name)
        conn.close()
        live_token = session_obj.token if used == "live/ts-pipe" else None

        async def _live_dlna_poll(target: Renderer, token: str | None) -> None:
            ends = 0
            while active_play is not None:
                await asyncio.sleep(15)
                try:
                    state = await playback_state(target)
                except Exception:
                    state = "STOPPED"
                if state in ("STOPPED", "NoMediaPresent", "IDLE"):
                    ends += 1
                    if ends >= 2:
                        if active_play is not None:
                            await _stop_active("live playback ended")
                        return
                else:
                    ends = 0

        active_play = ActivePlay(play_id=play_id, renderer=renderer, tv_name=renderer.friendly_name, media_key=None,
                                 task=asyncio.create_task(_live_dlna_poll(renderer, live_token)),
                                 proxy_token=live_token)
        session["last_tv_udn"] = renderer.udn
        await _send_controls(chat_id, payload["title"], renderer.friendly_name)
        return result + "\n🎛 mode: live/direct"

    conn = store.connect()
    lib = store.get_library_item(conn, payload["media_key"])
    conn.close()
    library_url = None
    if lib and lib["status"] == "ready" and lib["path"]:
        lib_path = Path(lib["path"])
        if lib_path.exists():
            library_url = f"http://{proxy.lan_ip_for(tv_ip)}:{proxy.PROXY_PORT}/library/{lib_path.relative_to(library.LIBRARY_DIR)}"

    attempts = []
    if library_url:
        attempts.append(("library", "copy"))
    if prefer_proxy:
        if virtual_channel:
            attempts.append(("virtual", "transcode"))
        if downgrade:
            attempts.append(("cache", "downgrade"))
        attempts.append(("cache", "copy"))
        if downgrade:
            attempts.append(("proxy", "downgrade"))
        attempts.append(("proxy", "copy"))
    else:
        attempts.append(("direct", "copy"))
        if downgrade:
            attempts.append(("proxy", "downgrade"))
        attempts.append(("proxy", "copy"))

    state = "STOPPED"
    used = None
    wedge_errors = 0
    control_timeouts = 0
    if provider_health()["down"]:
        lib_only = [a for a in attempts if a[0] == "library"]
        if lib_only:
            attempts = lib_only
    link_token = session.get("link_token")
    link_live = proxy.get_session(link_token) if link_token else None
    if link_live is not None and not link_live.done and attempts:
        attempts = [("link", "copy")] + attempts
    for mode, quality in attempts:
        try:
            await stop(renderer)
            for _ in range(8):
                if await playback_state(renderer) == "STOPPED":
                    break
                await asyncio.sleep(1)
            await asyncio.sleep(2)
        except Exception:
            pass
        url, ext = payload["url"], payload["ext"]
        duration_s = None
        if mode == "link":
            session_obj = proxy.get_session(session.get("link_token"))
            if session_obj is None or session_obj.done:
                raise RuntimeError("link session ended")
            proxy_token = session_obj.token
            url, ext = proxy.proxied_url(session_obj.token, tv_ip), "ts"
            duration_s = session_obj.duration_s
        elif mode == "library":
            url = library_url
            ext = "mkv"
            duration_s = await probe_duration(lib_path)
        elif mode == "virtual":
            try:
                session_obj = await proxy.start_virtual_session(
                    payload["url"], payload["title"],
                    transcode=True,
                    start_offset=session.get("resume_pos") or 0.0,
                    audio_index=session.get("audio_index"),
                )
            except Exception as exc:
                logger.warning("attempt %s/%s failed: %s", mode, quality, exc)
                telemetry.emit("cast", "attempt", "failed", mode=mode, quality=quality, error=str(exc)[:120])
                continue
            proxy_token = session_obj.token
            url, ext = proxy.proxied_url(session_obj.token, tv_ip), "ts"
            duration_s = session_obj.duration_s
        elif mode == "cache":
            try:
                session_obj = await proxy.start_session(
                    payload["url"], payload["title"],
                    mode=f"cache_{quality}" if quality == "downgrade" else "cache",
                    mime="video/mpeg",
                    start_offset=session.get("resume_pos") or 0.0,
                    audio_index=session.get("audio_index"),
                    sub_index=session.get("sub_index"),
                )
            except Exception as exc:
                logger.warning("attempt %s/%s failed: %s", mode, quality, exc)
                telemetry.emit("cast", "attempt", "failed", mode=mode, quality=quality, error=str(exc)[:120])
                continue
            proxy_token = session_obj.token
            try:
                await asyncio.wait_for(session_obj.ready.wait(), timeout=20)
            except asyncio.TimeoutError:
                primed = session_obj.path is not None and session_obj.path.exists() and session_obj.path.stat().st_size > 0
                if not primed:
                    logger.error("cache session %s empty after 20s — provider busy or down, aborting cast", session_obj.token)
                    telemetry.emit("cast", "attempt", "aborted", reason="session never primed", mode=mode, quality=quality)
                    try:
                        await proxy.stop_session(session_obj.token)
                    except Exception:
                        logger.debug("failed to stop unprimed session", exc_info=True)
                    return (
                        "⚠️ Couldn't pull the stream from the provider (busy or down) — nothing was cast. "
                        "Try again in a minute."
                    )
                logger.warning("cache session %s priming slowly — casting anyway", session_obj.token)
            url = proxy.proxied_url(session_obj.token, tv_ip)
            ext = "ts"
        elif mode == "proxy":
            session_obj = await proxy.start_session(
                payload["url"], payload["title"], mode=quality,
                start_offset=session.get("resume_pos") or 0.0,
                audio_index=session.get("audio_index"),
                sub_index=session.get("sub_index"),
            )
            proxy_token = session_obj.token
            url = proxy.proxied_url(session_obj.token, tv_ip)
            ext = "ts"
        try:
            state = await cast(renderer, url, payload["title"], ext, duration_s=duration_s, protocol_info=_protocol_info_for(renderer.friendly_name, ext))
            waits = 0
            while state in ("TRANSITIONING", "STOPPED") and waits < 6:
                await asyncio.sleep(5)
                waits += 1
                try:
                    state = await playback_state(renderer)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("attempt %s/%s failed: %s", mode, quality, exc)
            telemetry.emit("cast", "attempt", "failed", mode=mode, quality=quality, error=str(exc)[:120])
            if "701" in str(exc) or "716" in str(exc) or "705" in str(exc):
                wedge_errors += 1
            timeout_hit = "TimeoutError" in str(exc) or "UpnpConnection" in str(exc)
            if mode == "virtual":
                await asyncio.sleep(12)
                try:
                    await cast(renderer, url, payload["title"], ext, protocol_info=_protocol_info_for(renderer.friendly_name, ext))
                except Exception as exc2:
                    logger.warning("virtual kick-cast ignored: %s", exc2)
                state = "STOPPED"
            elif timeout_hit:
                control_timeouts += 1
                state = "STOPPED"
            else:
                await asyncio.sleep(8)
                try:
                    state = await cast(renderer, url, payload["title"], ext, protocol_info=_protocol_info_for(renderer.friendly_name, ext))
                    waits = 0
                    while state in ("TRANSITIONING", "STOPPED") and waits < 6:
                        await asyncio.sleep(5)
                        waits += 1
                        try:
                            state = await playback_state(renderer)
                        except Exception:
                            pass
                except Exception as exc2:
                    logger.warning("attempt %s/%s retry failed: %s", mode, quality, exc2)
                    if "701" in str(exc2) or "716" in str(exc2) or "705" in str(exc2):
                        wedge_errors += 1
                    state = "STOPPED"
        if state != "PLAYING" and proxy_token:
            s_live = proxy.get_session(proxy_token)
            loop_t = asyncio.get_running_loop().time
            if s_live and not s_live.done and s_live.path and s_live.path.exists() and s_live.last_client_seen > 0:
                served_a = s_live.max_served
                deadline = loop_t() + (75 if mode == "virtual" else 40)
                while loop_t() < deadline:
                    await asyncio.sleep(3)
                    if s_live.max_served > served_a:
                        state = "PLAYING"
                        control_timeouts = 0
                        logger.warning(
                            "attempt %s/%s: control plane unreliable but TV consumed bytes (%d->%d) — treating as playing",
                            mode, quality, served_a, s_live.max_served,
                        )
                        break
                if state != "PLAYING":
                    logger.warning("attempt %s/%s: TV made contact but never consumed bytes in time — failing over", mode, quality)
                elif timeout_hit:
                    control_timeouts += 1
                    if control_timeouts >= 2:
                        state = "STOPPED"
                        break
        if state == "PLAYING":
            used = f"{mode}/{quality}"
            break
        if proxy_token and mode in ("cache", "proxy", "virtual"):
            try:
                await proxy.stop_session(proxy_token)
            except Exception:
                pass
            proxy_token = None
            logger.info("waiting 30s for the provider slot to free before the next attempt")
            await asyncio.sleep(30)
        if control_timeouts >= 2:
            logger.warning("TV DLNA stack unresponsive (%d consecutive control timeouts) — aborting cast; power-cycle the TV", control_timeouts)
            break
        if wedge_errors >= 3:
            logger.warning("TV DLNA stack looks wedged (%d consecutive 701/716) — aborting remaining attempts", wedge_errors)
            break

    result = f"{pre}📺 <b>{renderer.friendly_name}</b> → “{payload['title']}”\nPlayer state: <b>{state}</b>"
    if state == "STOPPED" and control_timeouts >= 2:
        result += (
            "\n\n🛑 The TV stopped responding during the cast (control plane timeout). "
            "Power-cycle the TV — unplug for 30 seconds — then try again."
        )
        return result
    if state == "STOPPED" and wedge_errors >= 3:
        result += (
            "\n\n🛑 The TV's player looks stuck (it refused every mode). "
            "Power-cycle the TV — unplug it for 30 seconds, turn it back on, then try again."
        )
        return result
    if state == "STOPPED":
        result += f"\n\n⚠️ TV refused every mode (last: {used}). If this file is 4K-HEVC, this TV may not decode it."
        return result

    resume_pos = session.get("resume_pos")
    session["resume_pos"] = None
    if resume_pos and used and used.startswith("direct"):
        await asyncio.sleep(2)
        if await seek(renderer, resume_pos):
            result += f"\n⏩ Resumed at {_fmt_time(resume_pos)}"
        else:
            result += "\n⚠️ TV doesn't support seeking — playing from the start."
    elif resume_pos:
        result += f"\n⏩ Started at {_fmt_time(resume_pos)}"

    conn = store.connect()
    play_id = store.start_play(
        conn, user_id, payload["kind"], payload["ref_id"], payload["media_key"],
        payload["title"], payload.get("lang"), renderer.friendly_name,
    )
    conn.close()
    task = asyncio.create_task(_poll_playback_dlna(play_id, renderer, user_id, payload["media_key"], proxy_token))
    active_play = ActivePlay(play_id=play_id, renderer=renderer, tv_name=renderer.friendly_name, media_key=payload["media_key"], task=task, proxy_token=proxy_token)
    session["last_tv_udn"] = renderer.udn
    result += f"\n🎛 mode: {used}"

    def _subs_note() -> str:
        conn2 = store.connect()
        try:
            if payload["kind"] == "episode":
                row = conn2.execute("SELECT probe_subs FROM episodes WHERE episode_id=?", (payload["ref_id"],)).fetchone()
            else:
                row = conn2.execute("SELECT probe_subs FROM movies WHERE stream_id=?", (payload["ref_id"],)).fetchone()
        finally:
            conn2.close()
        if row and row["probe_subs"]:
            note = f"💬 Subtitles on file: {row['probe_subs']} — toggle with your TV remote"
            if used == "proxy/copy":
                note += " (not available in this stream mode)"
            return note
        return ""

    await _send_controls(chat_id, payload["title"], renderer.friendly_name, _subs_note())
    return result


def _build_payload(group: Group, variant) -> dict:
    if group.kind == "movie":
        return {
            "kind": "movie",
            "ref_id": variant.ref_id,
            "media_key": f"movie:{variant.ref_id}",
            "title": f"{group.title} [{_lang_label(variant)}]",
            "lang": variant.lang,
            "url": provider.movie_url(variant.ref_id, variant.ext),
            "ext": variant.ext,
        }
    return {
        "kind": "series",
        "ref_id": variant.ref_id,
        "media_key": f"series:{variant.ref_id}",
        "title": group.title,
        "lang": variant.lang,
        "url": "",
        "ext": "mkv",
    }


async def _offer_resume(send, session: dict, payload: dict, user_id: int) -> bool:
    if session.get("resume_pos") is not None:
        return False
    conn = store.connect()
    progress = store.get_progress(conn, user_id, payload["media_key"])
    conn.close()
    if not progress or progress["position_s"] < 30:
        return False
    duration = progress["duration_s"] or 0
    pos = float(progress["position_s"])
    if duration and pos >= duration * 0.95:
        return False
    if duration:
        pos = min(pos, max(0.0, duration - 20))
    session["pending"] = payload
    session["resume_pos"] = pos
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"▶️ Resume at {_fmt_time(progress['position_s'])}", callback_data="go:resume"),
                InlineKeyboardButton(text="⏱ Start over", callback_data="go:start"),
            ]
        ]
    )
    await send.answer(
        f"⏸ You watched this to <b>{_fmt_time(progress['position_s'])}</b> — continue?",
        reply_markup=keyboard,
    )
    return True


async def _probe_subs(bot_ref, chat_id: int, kind: str, ref_id: int, url: str) -> None:
    try:
        from .subs import probe_and_cache

        langs = await probe_and_cache(kind, ref_id, url)
        if langs:
            await bot_ref.send_message(
                chat_id,
                f"📼 Embedded subtitle tracks: <b>{', '.join(langs)}</b> — switch with your remote's Subtitle button.",
            )
    except Exception:
        logger.exception("probe failed")


def _progress_title(conn, media_key: str) -> str:
    return store.progress_title(conn, media_key)


_bot: Bot | None = None
_last_user_activity = time.time()


def _controls_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏪60", callback_data="ctl:seek:-60"),
            InlineKeyboardButton(text="⏪30", callback_data="ctl:seek:-30"),
            InlineKeyboardButton(text="⏸", callback_data="ctl:pause"),
            InlineKeyboardButton(text="⏩30", callback_data="ctl:seek:30"),
            InlineKeyboardButton(text="⏩60", callback_data="ctl:seek:60"),
        ],
        [
            InlineKeyboardButton(text="🔉−", callback_data="ctl:vol:-10"),
            InlineKeyboardButton(text="🔇", callback_data="ctl:mute"),
            InlineKeyboardButton(text="🔊+", callback_data="ctl:vol:10"),
            InlineKeyboardButton(text="🔄 position", callback_data="ctl:refresh"),
            InlineKeyboardButton(text="⏹ stop", callback_data="ctl:stop"),
        ],
    ])

async def _send_controls(chat_id: int, title: str, tv_name: str, subs_note: str = "") -> None:
    global active_play
    if active_play is None:
        return
    text = f"▶️ <b>{title}</b>\n📺 {tv_name}"
    if subs_note:
        text += f"\n{subs_note}"
    msg = await _bot.send_message(chat_id, text, reply_markup=_controls_keyboard())
    active_play.chat_id = chat_id
    active_play.message_id = msg.message_id
    active_play.title = title

async def _active_position() -> tuple[float, float] | None:
    if active_play is None:
        return None
    if active_play.proxy_token:
        s = proxy.get_session(active_play.proxy_token)
        merged = proxy.progress_seconds(s) if s else None
        if merged:
            return merged
    target = active_play.renderer
    if isinstance(target, CastTarget):
        from .cc import media_status
        ms = media_status(target.ip)
        if ms is None:
            return None
        return ms[1], ms[2]
    try:
        pos, dur = await get_position(target)
        return pos, dur
    except Exception:
        return None

async def _refresh_controls() -> None:
    if active_play is None or active_play.chat_id is None or active_play.message_id is None:
        return
    pd = await _active_position()
    pos_txt = ""
    if pd:
        pos_txt = f" · {_fmt_time(pd[0])}" + (f"/{_fmt_time(pd[1])}" if pd[1] else "")
    state = "⏸ paused" if active_play.paused else "▶️ playing"
    try:
        await _bot.edit_message_text(
            f"{state} <b>{active_play.title}</b>\n📺 {active_play.tv_name}{pos_txt}",
            chat_id=active_play.chat_id, message_id=active_play.message_id,
            reply_markup=_controls_keyboard(),
        )
    except TelegramBadRequest:
        pass




async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    class _RedactProviderUrls(logging.Filter):
        _creds = re.compile(r"((?:movie|series|live)/)[^/\s]+/[^/\s]+/")
        _userinfo = re.compile(r"//[^/@\s]+:[^/@\s]+@")

        def filter(self, record):
            try:
                msg = record.getMessage()
            except Exception:
                return True
            redacted = self._creds.sub(r"\1***/***/", msg)
            redacted = self._userinfo.sub("//***:***@", redacted)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
            return True

    redactor = _RedactProviderUrls()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    global _bot
    dp = Dispatcher()
    _bot = bot

    @dp.errors()
    async def _swallow_benign(event) -> bool:
        exception = getattr(event, "exception", None)
        if isinstance(exception, TelegramBadRequest) and (
            "message is not modified" in str(exception) or "query is too old" in str(exception)
        ):
            return True
        logger.exception("unhandled update error: %s", exception)
        return True

    async def warm() -> None:
        try:
            from . import embedder
            from .search import _get_index

            await asyncio.to_thread(embedder.encode, ["warmup"])
            await asyncio.to_thread(_get_index)
            logger.info("search index warmed up")
        except Exception:
            logger.exception("warmup failed")

    class AuthMiddleware(BaseMiddleware):
        async def __call__(self, handler, event, data):
            global _last_user_activity
            _last_user_activity = time.time()
            user: User | None = data.get("event_from_user")
            if user is None:
                return await handler(event, data)
            if not is_allowed(user.id):
                if isinstance(event, Message):
                    await event.answer(
                        f"⛔ Not authorized.\nYour Telegram ID: <code>{user.id}</code>\n"
                        "The first person to /start claims this bot; others must be added in the web panel."
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer("Not authorized.", show_alert=True)
                return
            return await handler(event, data)

    dp.update.outer_middleware(AuthMiddleware())

    async def run_search(message: Message, status: Message, query: dict) -> None:
        wants = " · ".join(
            filter(
                None,
                [
                    query.get("media_type"),
                    query.get("language"),
                    str(query.get("year")) if query.get("year") else None,
                    f"subs:{query['subtitles']}" if query.get("subtitles") else None,
                ],
            )
        )
        if provider_health()["down"]:
            words = _title_words(query["title"])
            conn = store.connect()
            rows = conn.execute(
                "SELECT media_key, title FROM library WHERE status='ready' AND path IS NOT NULL ORDER BY rowid DESC"
            ).fetchall()
            conn.close()
            matches = [r for r in rows if not words or (words & _title_words(r["title"]))]
            kb_rows = [
                [InlineKeyboardButton(text=f"▶️ {r['title'][:48]}", callback_data=f"plib:{r['media_key']}")]
                for r in matches[:10]
            ]
            note = f"🛑 <b>Provider offline</b> — showing your downloaded library ({len(matches)} match(es)). Watchlist downloads resume automatically when it's back."
            if kb_rows:
                await status.edit_text(note, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
            else:
                await status.edit_text(note + "\nNo local matches yet.")
            return
        await status.edit_text(f"🔎 Searching <b>{query['title']}</b> {wants}…")
        groups = await asyncio.to_thread(search_media, query, MAX_CANDIDATES)
        if not groups:
            await status.edit_text("Nothing close enough. Try the English or French title, or check the year.")
            return
        session = _session(message.chat.id)
        session["groups"] = groups
        session["pending"] = None
        await status.edit_text(f"Found {len(groups)} candidate(s) — tap the right one:")
        for gi, group in enumerate(groups):
            await _send_group(message, gi, group, f"match {group.best_score:.2f} · {group.kind}")

    @dp.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "🎬 <b>VOD-Delivery</b>\n\n"
            "Just tell me what you want to watch:\n"
            "<i>“i need to watch the movie greenland 2020 english with arabic sub”</i>\n\n"
            "/tvs — find and pick a TV\n"
            "/continue — continue watching\n"
            "/watchlist — saved titles\n"
            "/stop — stop playback\n"
            "/token — create an app token\n"
            "/tokens — manage app tokens\n"
            "/ping — alive check\n\n"
            "Web panel: <code>http://"
            + (proxy.lan_ip_for("192.168.1.1") or "localhost")
            + ":8095</code>"
        )

    @dp.message(Command("ping"))
    async def cmd_ping(message: Message) -> None:
        await message.answer("pong 🏓")

    @dp.message(Command("token"))
    async def cmd_token(message: Message) -> None:
        label = (message.text or "").partition(" ")[2].strip() or f"telegram:{message.from_user.id}"
        conn = store.connect()
        try:
            token = store.issue_app_token(conn, label[:80])
        finally:
            conn.close()
        await message.answer(
            "🔑 <b>New app token</b>\n"
            f"<code>{token}</code>\n\n"
            f"Label: <b>{label[:80]}</b>\n"
            "Paste it in the Android app's server settings, or pair the device from the App access page."
        )

    @dp.message(Command("tokens"))
    async def cmd_tokens(message: Message) -> None:
        conn = store.connect()
        try:
            rows = store.list_app_tokens(conn)
        finally:
            conn.close()
        if not rows:
            await message.answer("No app tokens yet. Send <code>/token phone</code> to create one.")
            return
        lines = ["🔑 <b>App tokens</b>", ""]
        kb = []
        for r in rows:
            mask = f"{r['token'][:6]}…{r['token'][-4:]}"
            label = r["label"] or "unnamed"
            lines.append(f"• <b>{label}</b> · <code>{mask}</code> · seen {r['last_seen'] or 'never'}")
            kb.append(
                [
                    InlineKeyboardButton(
                        text=f"🗑 Revoke {label}"[:60],
                        callback_data=f"tok:revoke:{r['token']}",
                    )
                ]
            )
        await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    @dp.callback_query(F.data.startswith("tok:revoke:"))
    async def cb_token_revoke(query: CallbackQuery) -> None:
        token = (query.data or "").split(":", 2)[2]
        conn = store.connect()
        try:
            store.revoke_app_token(conn, token)
        finally:
            conn.close()
        await query.answer("Token revoked")
        await query.message.edit_text("🗑 Token revoked.")

    @dp.callback_query(F.data.startswith("pair:"))
    async def cb_pair_decision(query: CallbackQuery) -> None:
        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            await query.answer()
            return
        _, action, request_id = parts
        conn = store.connect()
        try:
            row = store.get_pair_request(conn, request_id)
            if row is None or row["status"] != "pending":
                await query.answer("Already handled or expired.", show_alert=True)
                return
            name = row["device_name"] or "device"
            if action == "ok":
                store.approve_pair_request(conn, request_id)
                text = f"✅ Approved <b>{name}</b>. The device will collect its token on the next check."
            else:
                store.deny_pair_request(conn, request_id)
                text = f"⛔ Denied <b>{name}</b>."
        finally:
            conn.close()
        await query.answer()
        await query.message.edit_text(text)

    @dp.message(Command("tvs"))
    async def cmd_tvs(message: Message) -> None:
        wait = await message.answer("🔎 Scanning network for TVs…")
        renderers = await _discover_merged()
        session = _session(message.chat.id)
        session["renderers"] = renderers
        session["selected"] = None
        if not renderers:
            await wait.edit_text("No DLNA TVs found. Power them on (standby usually works), then /tvs again.")
            return
        names = "\n".join(f"• {r.friendly_name}" for r in renderers)
        await wait.edit_text(
            f"Found {len(renderers)} TV(s):\n{names}\n\nPick the one to cast to (rename in the web panel).",
            reply_markup=_tv_keyboard(session),
        )

    @dp.message(Command("bantv"))
    async def cmd_bantv(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            await message.answer("Usage: <code>/bantv &lt;MAC or TV name&gt;</code>\nBanned TVs never show up in /tvs, scans, the web panel or the app.")
            return
        conn = store.connect()
        try:
            mac = store.normalize_mac(target)
            row = None
            if not mac:
                row = store.find_tv_by_name(conn, target)
                mac = store.normalize_mac(row["mac"]) if row and row["mac"] else None
            if not mac:
                await message.answer("Could not resolve a MAC. Send the MAC address (AA:BB:CC:DD:EE:FF).")
                return
            store.ban_tv(conn, mac, row["name"] if row else target)
        finally:
            conn.close()
        await message.answer(f"🚫 Banned <code>{mac}</code> — hidden from /tvs, scans, the panel and the app.")

    @dp.message(Command("unbantv"))
    async def cmd_unbantv(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            await message.answer("Usage: <code>/unbantv &lt;MAC&gt;</code>")
            return
        conn = store.connect()
        try:
            mac = store.normalize_mac(target)
            if not mac:
                await message.answer("Send the MAC address (AA:BB:CC:DD:EE:FF).")
                return
            store.unban_tv(conn, mac)
        finally:
            conn.close()
        await message.answer(f"✅ Unbanned <code>{mac}</code>.")

    @dp.message(Command("audio"))
    async def cmd_audio(message: Message) -> None:
        if active_play is None or not getattr(active_play, "media_key", None):
            await message.answer("Nothing is playing.")
            return
        media_key = active_play.media_key
        from . import trackprobe

        data = trackprobe.get_cached(media_key)
        if not data:
            await message.answer(
                "No track data cached for this title yet.\n"
                "Open it once in the app (Play sheet) so the server probes it, then try again."
            )
            return
        audio = data.get("audio") or []
        if not audio:
            await message.answer("This file reports a single audio stream.")
            return
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            lines = "\n".join(
                f"{i + 1}. {a.get('lang') or '?'} · {a.get('codec') or ''} · {a.get('channels') or '?'}ch"
                + (" (default)" if a.get("default") else "")
                for i, a in enumerate(audio)
            )
            await message.answer(
                "🎧 <b>Audio tracks</b>\n" + lines +
                "\n\nSwitch with <code>/audio &lt;n&gt;</code> — re-casts at the current position."
            )
            return
        try:
            track = audio[int(arg) - 1]
        except (ValueError, IndexError):
            await message.answer("Pick a number from /audio.")
            return
        payload = await _resolve_media_key(media_key)
        if not payload:
            await message.answer("Could not resolve this title.")
            return
        pos = 0.0
        token = getattr(active_play, "proxy_token", None)
        if token:
            s = proxy.get_session(token)
            merged = proxy.progress_seconds(s) if s else None
            pos = merged[0] if merged else 0.0
        else:
            pd = await _active_position()
            pos = pd[0] if pd else 0.0
        renderer = active_play.renderer
        session = _session(message.chat.id)
        session["pending"] = payload
        session["resume_pos"] = pos
        session["audio_index"] = int(track["index"])
        session["sub_index"] = None
        session["renderers"] = [renderer]
        session["selected"] = 0
        wait = await message.answer(
            f"🔁 Switching to <b>{track.get('lang') or track.get('index')}</b> at {_fmt_time(pos)}…"
        )
        try:
            result = await _execute_cast(session, message.chat.id, 0)
        except Exception as exc:
            logger.exception("audio switch failed")
            result = f"❌ {type(exc).__name__}: {str(exc)[:160]}"
        session["pending"] = None
        await wait.edit_text(result)

    @dp.message(Command("stop"))
    async def cmd_stop(message: Message) -> None:
        session = _session(message.chat.id)
        stopped_note = await _stop_active("manual stop")
        if session.get("selected") is not None:
            renderer = session["renderers"][session["selected"]]
            try:
                await stop(renderer)
                state = await playback_state(renderer)
                stopped_note = f"⏹ Stopped on <b>{renderer.friendly_name}</b> (state: {state})"
            except Exception as exc:
                stopped_note = f"❌ Stop failed: <code>{type(exc).__name__}: {exc}</code>"
        await message.answer(stopped_note or "Nothing playing.")

    @dp.message(Command("watchlist"))
    async def cmd_watchlist(message: Message) -> None:
        conn = store.connect()
        items = store.list_watchlist(conn, message.from_user.id)
        conn.close()
        if not items:
            await message.answer("Watchlist is empty. Tap ➕ on any search result.")
            return
        rows = [
            [InlineKeyboardButton(text=f"📌 {i['title']} ({i['year'] or '?'})", callback_data=f"wlq:{i['kind']}:{i['ref_id']}")]
            for i in items[:10]
        ]
        await message.answer("Your watchlist:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    def _next_episode_row(series_id: int, cur_ep_id: int):
        conn = store.connect()
        try:
            rows = conn.execute(
                "SELECT episode_id, ext, season, episode, title FROM episodes WHERE series_id=?"
                " ORDER BY season, episode, episode_id",
                (series_id,),
            ).fetchall()
        finally:
            conn.close()
        for i, row in enumerate(rows):
            if row["episode_id"] == cur_ep_id and i + 1 < len(rows):
                return rows[i + 1]
        return None

    def _ended_keyboard(media_key: str) -> InlineKeyboardMarkup | None:
        if not media_key.startswith("episode:"):
            return None
        parts = media_key.split(":")
        if len(parts) < 3:
            return None
        try:
            series_id, ep_id = int(parts[2]), int(parts[1])
        except ValueError:
            return None
        nxt = _next_episode_row(series_id, ep_id)
        if nxt is None:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"▶️ Next: S{nxt['season']}E{nxt['episode']}",
                callback_data=f"nxt:{series_id}:{nxt['episode_id']}",
            )
        ]])

    async def _server_side_seek(new_pos: float, target) -> bool:
        if not active_play.proxy_token:
            return False
        udn = getattr(target, "udn", None)
        conn = store.connect()
        try:
            row = store.get_tv(conn, udn) if udn else None
        finally:
            conn.close()
        if row and row["v2_seek"]:
            ok, detail = await proxy.seek_session(active_play.proxy_token, new_pos)
            logger.info("append-seek to %.0fs on %s -> %s (%s)", new_pos, target.friendly_name, "ok" if ok else "failed", detail)
            return ok
        s = proxy.get_session(active_play.proxy_token)
        if s is None:
            return False
        url, title, mode, mime = s.provider_url, s.title, s.mode, s.mime
        try:
            await proxy.stop_session(active_play.proxy_token)
        except Exception:
            pass
        try:
            new_s = await proxy.start_session(url, title, mode=mode, mime=mime, start_offset=max(0.0, new_pos))
        except Exception:
            logger.exception("re-cast seek: respawn failed")
            return False
        active_play.proxy_token = new_s.token
        try:
            await asyncio.wait_for(new_s.ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            if not (new_s.path and new_s.path.exists() and new_s.path.stat().st_size > 0):
                return False
        state = await cast(target, proxy.proxied_url(new_s.token, target.ip), title, "ts", protocol_info=_protocol_info_for(target.friendly_name, "ts"))
        for _ in range(8):
            if state == "PLAYING":
                break
            await asyncio.sleep(3)
            try:
                state = await playback_state(target)
            except Exception:
                pass
        logger.info("re-cast seek to %.0fs on %s -> %s", new_pos, target.friendly_name, state)
        return state == "PLAYING"

    async def cb_controls(query: CallbackQuery) -> None:
        if active_play is None:
            await query.answer("Nothing is playing", show_alert=False)
            return
        parts = query.data.split(":")
        action = parts[1]
        target = active_play.renderer
        try:
            if action == "seek":
                pd = await _active_position()
                if active_play.proxy_token:
                    s = proxy.get_session(active_play.proxy_token)
                    merged = proxy.progress_seconds(s) if s else None
                    if merged:
                        pd = (merged[0], merged[1])
                base = abs(int(parts[2]))
                direction = 1 if int(parts[2]) > 0 else -1
                if pd is None:
                    await query.answer("Can't read the player position")
                    return
                now = time.time()
                state = _seek_state.get(query.message.chat.id)
                if state and state["dir"] == direction and now - state["ts"] <= 1.4:
                    idx = min(state["idx"] + 1, len(_SEEK_STEPS) - 1)
                else:
                    idx = 0
                    while idx < len(_SEEK_STEPS) - 1 and _SEEK_STEPS[idx] < base:
                        idx += 1
                step = _SEEK_STEPS[idx]
                _seek_state[query.message.chat.id] = {"dir": direction, "idx": idx, "ts": now}
                new_pos = max(0.0, (pd[0] or 0) + step * direction)
                if isinstance(target, CastTarget):
                    from .cc import seek as cc_seek
                    ok = cc_seek(target.ip, new_pos)
                else:
                    try:
                        _st = await playback_state(target)
                    except Exception:
                        _st = None
                    if _st in ("PLAYING", "PAUSED_PLAYBACK", "PAUSED"):
                        ok = await seek(target, new_pos)
                    else:
                        ok = False
                    if not ok and active_play.proxy_token and not active_play.seeking:
                        active_play.seeking = True
                        try:
                            ok = await _server_side_seek(new_pos, target)
                        except Exception as exc:
                            logger.warning("server-side seek crashed: %s", str(exc)[:120])
                            ok = False
                        finally:
                            active_play.seeking = False
                telemetry.emit("playback", "seek", "ok" if ok else "failed", step=step, chat=query.message.chat.id)
                await query.answer(("⏩ +%ds" % step) if direction > 0 else ("⏪ -%ds" % step))
                await asyncio.sleep(1.5)
                await _refresh_controls()
                return
            if action == "vol":
                delta = int(parts[2])
                if isinstance(target, CastTarget):
                    from .cc import volume as cc_volume
                    result = cc_volume(target.ip, delta / 100.0)
                    ok = result is not None
                else:
                    from .caster import volume_step
                    result = await volume_step(target, delta)
                    ok = result is not None
                telemetry.emit("playback", "volume", "ok" if ok else "failed", delta=delta)
                await query.answer(f"🔊 {delta:+d}" + (f" → {result}" if result is not None else ""))
                return
            if action == "mute":
                new_muted = not active_play.muted
                if isinstance(target, CastTarget):
                    from .cc import volume as cc_volume
                    result = cc_volume(target.ip, mute=new_muted)
                    ok = result is not None
                else:
                    from .caster import set_mute
                    ok = await set_mute(target, new_muted)
                if ok:
                    active_play.muted = new_muted
                telemetry.emit("playback", "mute", "ok" if ok else "failed", muted=new_muted)
                await query.answer("🔇 muted" if new_muted else "🔊 unmuted")
                return
            if action == "pause":
                if isinstance(target, CastTarget):
                    from .cc import pause as cc_pause, resume as cc_resume
                    ok = cc_resume(target.ip) if active_play.paused else cc_pause(target.ip)
                else:
                    from .caster import pause as dlna_pause, resume as dlna_resume
                    ok = await dlna_resume(target) if active_play.paused else await dlna_pause(target)
                if ok:
                    active_play.paused = not active_play.paused
                await query.answer("⏸ paused" if active_play.paused else "▶️ resumed")
                await _refresh_controls()
                return
            if action == "refresh":
                await query.answer("Refreshing…")
                await _refresh_controls()
                return
            if action == "stop":
                await query.answer("Stopping…")
                chat_id = active_play.chat_id
                msg_id = active_play.message_id
                await _stop_active("stopped from Telegram")
                if chat_id and msg_id:
                    try:
                        await _bot.edit_message_text(
                            f"⏹ Stopped", chat_id=chat_id, message_id=msg_id,
                        )
                    except TelegramBadRequest:
                        pass
                return
        except Exception:
            logger.exception("control %s failed", action)
            await query.answer("⚠️ Control failed — check the TV", show_alert=True)

    @dp.callback_query(F.data.startswith("ctl:"))
    async def _ctl_route(query: CallbackQuery) -> None:
        await cb_controls(query)

    async def _audit_worker() -> None:
        global _last_user_activity
        while True:
            await asyncio.sleep(60)
            try:
                idle_for = time.time() - _last_user_activity
                if idle_for < 300:
                    continue
                findings = await auditor.run_audit(force=False)
                for f in findings:
                    if str(f.get("severity", "")).lower() == "high" and f.get("notify", True):
                        await _notify_owner(
                            f"🕵️ Audit found a high-severity issue:\n"
                            f"<b>{f.get('feature','?')}</b>: {f.get('observation','')[:300]}\n"
                            f"Cause: {f.get('likely_cause','?')[:200]}"
                        )
                        break
            except Exception:
                logger.exception("audit worker cycle failed")

    def _provider_down_note() -> str:
        if provider_health()["down"]:
            return "\n⚠️ <i>Provider looks unreachable — already-downloaded library items keep playing (web panel → Library).</i>"
        return ""

    async def _provider_health_watch() -> None:
        was_down = False
        cycles = 0
        while True:
            await asyncio.sleep(60)
            cycles += 1
            if cycles % 60 == 0:
                try:
                    conn = store.connect()
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.close()
                except Exception:
                    logger.exception("nightly wal checkpoint failed")
                try:
                    down = provider_health()["down"]
                    if down and not was_down:
                        h = provider_health()
                        telemetry.emit("provider", "health", "down", failures=h["recent_failures"])
                        await _notify_owner(
                            "🛑 Provider unreachable "
                            f"({h['recent_failures']} failures/15 min, last: {h['last_error'][:120]})\n"
                            "If the subscription expired: library downloads keep playing locally."
                        )
                    if not down and was_down:
                        await _notify_owner("✅ Provider is back online.")
                    was_down = down
                except Exception:
                    logger.exception("provider health watch cycle failed")

    async def _notify_owner(text: str) -> None:
        owner = library.owner_chat_id()
        if owner:
            try:
                await _bot.send_message(owner, text)
            except Exception:
                logger.exception("owner notify failed")

    async def _cache_warning_check() -> None:
        import shutil as _shutil

        free = _shutil.disk_usage(library.LIBRARY_DIR).free
        if free < 15 * 1024**3:
            await _notify_owner(f"⚠️ Low disk space: <b>{free / 1024**3:.1f} GB</b> free.")
        usage = library.cache_usage_bytes()
        limit = library.cache_limit_bytes()
        conn = store.connect()
        try:
            warned = store.get_meta(conn, "cache_warned")
        finally:
            conn.close()
        if usage > limit and warned != "1":
            gb = usage / 1024**3
            await _notify_owner(f"⚠️ Cache is at <b>{gb:.1f} GB</b> (limit {limit/1024**3:.0f} GB).\nClean up in the web panel → Library.")
            conn = store.connect()
            store.set_meta(conn, "cache_warned", "1")
            conn.close()
        elif usage < limit and warned == "1":
            conn = store.connect()
            store.set_meta(conn, "cache_warned", "0")
            conn.close()

    async def _library_worker() -> None:
        global active_play
        idle_gap = 15 * 60
        next_run = 0.0
        while True:
            await asyncio.sleep(30)
            try:
                if active_play is not None or proxy.active_session() is not None:
                    continue
                if provider_health()["down"]:
                    continue
                if library.cache_usage_bytes() > library.cache_limit_bytes():
                    logger.info("library over its size limit — deferring downloads")
                    continue
                now = asyncio.get_running_loop().time()
                if now < next_run:
                    continue
                await library.sync_watchlist_queue()
                conn = store.connect()
                item = store.dequeue_next(conn)
                conn.close()
                if item is None:
                    continue
                plan = library.plan_for(item["kind"], item["ref_id"], item["ext"])
                if plan is None:
                    conn = store.connect()
                    store.set_queue_status(conn, item["media_key"], "failed", "metadata missing")
                    conn.close()
                    continue
                rel_final = library.abs_path(plan["rel"])
                if rel_final.exists():
                    conn = store.connect()
                    library.register_download(item["media_key"], item["kind"], item["ref_id"], plan["title"], plan["rel"], rel_final.stat().st_size, plan, item["source"])
                    store.set_queue_status(conn, item["media_key"], "done")
                    conn.close()
                    continue
                if active_play is not None or proxy.active_session() is not None:
                    logger.info("library queue deferred — a playback session is active")
                    break
                logger.info("library download starting: %s", item["title"])
                s = await proxy.start_session(item["url"], item["title"], mode="cache", mime="video/x-matroska")
                conn = store.connect()
                store.set_queue_status(conn, item["media_key"], "downloading")
                conn.close()
                deadline = asyncio.get_running_loop().time() + 3 * 3600
                while not s.completed and not s.done:
                    await asyncio.sleep(5)
                    other = proxy.active_session()
                    if active_play is not None or (other is not None and other.token != s.token):
                        logger.info("viewing started — aborting library download: %s", item["title"])
                        await proxy.stop_session(s.token)
                        conn = store.connect()
                        store.set_queue_status(conn, item["media_key"], "queued", "deferred for live viewing")
                        conn.close()
                        s.done = True
                        break
                    if asyncio.get_running_loop().time() > deadline:
                        await proxy.stop_session(s.token)
                        conn = store.connect()
                        store.set_queue_status(conn, item["media_key"], "failed", "timeout")
                        conn.close()
                        s.done = True
                        break
                if s.completed and s.path and s.path.exists():
                    size = s.path.stat().st_size
                    final = library.finalize_download(s.path, plan["rel"])
                    asyncio.create_task(asyncio.to_thread(library.ensure_subs, plan["media_key"]))
                    library.register_download(item["media_key"], item["kind"], item["ref_id"], plan["title"], plan["rel"], size, plan, item["source"])
                    conn = store.connect()
                    store.set_queue_status(conn, item["media_key"], "done")
                    conn.close()
                    logger.info("library download done: %s (%.1f GB)", final.name, size / 1024**3)
                    await _notify_owner(f"📥 <b>{plan['title']}</b> is ready offline ({size/1024**3:.1f} GB).")
                    await _cache_warning_check()
                    next_run = asyncio.get_running_loop().time() + idle_gap + 5 * 60
                elif not s.completed:
                    conn = store.connect()
                    store.set_queue_status(conn, item["media_key"], "failed", "download ended incomplete")
                    conn.close()
                    next_run = asyncio.get_running_loop().time() + 5 * 60
            except Exception:
                logger.exception("library worker cycle failed")
                await asyncio.sleep(120)


    @dp.message(Command("continue"))
    async def cmd_continue(message: Message) -> None:
        await _send_continue_list(message)

    async def _send_next_episode(message: Message, title_hint: str | None = None) -> bool:
        hint_words = _title_words(title_hint or "") or _title_words(message.text or "")
        conn = store.connect()
        rows = conn.execute(
            "SELECT media_key FROM watch_progress WHERE user_id=? AND media_key LIKE 'episode:%' ORDER BY updated_at DESC LIMIT 10",
            (message.from_user.id,),
        ).fetchall()
        best = None
        for r in rows:
            _, _, ref = r["media_key"].partition(":")
            ep_id, _, series_ref = ref.partition(":")
            if not series_ref:
                continue
            row = conn.execute(
                "SELECT e.season, e.episode, s.title AS series_title, s.series_id FROM episodes e "
                "JOIN series s ON s.series_id=e.series_id WHERE e.episode_id=?",
                (int(ep_id),),
            ).fetchone()
            if row is None:
                continue
            if hint_words and not (_title_words(row["series_title"]) & hint_words):
                continue
            best = row
            break
        conn.close()
        if best is None:
            return False
        info = await provider.series_info(best["series_id"])
        eps = []
        for season_key, eplist in info.get("episodes", {}).items():
            for e in eplist:
                eps.append((int(season_key), int(e.get("episode_num", 0)), e))
        eps.sort(key=lambda x: (x[0], x[1]))
        last = (best["season"], best["episode"])
        nxt = next(((s_, n_, e_) for s_, n_, e_ in eps if (s_, n_) > last), None)
        if nxt is None:
            await message.answer(f"You've watched everything available of <b>{best['series_title']}</b> 🍿")
            return True
        s_, n_, e_ = nxt
        ep_name = (e_.get("title") or "").strip()
        label = f"▶ Next: {best['series_title']} — S{s_}E{n_}"
        if ep_name and not ep_name.upper().startswith("S0"):
            label += f" · {ep_name}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=label[:64],
                callback_data=f"ep:{e_['id']}:{e_.get('container_extension', 'mkv')}:{best['series_id']}:{s_}",
            )
        ]])
        await message.answer("📺 Ready when you are:", reply_markup=kb)
        return True

    async def _send_continue_list(message: Message) -> None:
        conn = store.connect()
        items = store.continue_watching(conn, message.from_user.id)
        titles = {i["media_key"]: _progress_title(conn, i["media_key"]) for i in items}
        conn.close()
        if not items:
            await message.answer("Nothing in progress. Start something!")
            return
        rows = []
        for i in items:
            pct = int(i["position_s"] / i["duration_s"] * 100) if i["duration_s"] else 0
            label = f"▶ {titles[i['media_key']]} · {pct}%"
            rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"cgo:{i['media_key']}")])
        await message.answer("▶️ Continue watching:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def _run_url_download(query: CallbackQuery, token: str) -> None:
        global _url_dl_active
        info = _URL_DL.get(token)
        if info is None:
            await query.answer("Expired — send the link again.", show_alert=True)
            return
        if _url_dl_active:
            await query.answer("Another download is already running.", show_alert=True)
            return
        _URL_DL.pop(token, None)
        _url_dl_active = True
        title = info["title"]
        try:
            dest = library._unique_dest(title, Path(info["title"]).suffix or ".mkv")

            async def _progress(pos: int, total: int) -> None:
                pct = int(pos / total * 100) if total else 0
                mb = pos / 1024**2
                try:
                    await query.message.edit_text(
                        f"⬇️ <b>{title}</b> — {pct}% ({mb:.0f} MB)"
                        + (f" / {total/1024**2:.0f} MB" if total else "")
                    )
                except Exception:
                    pass

            await query.message.edit_text(f"⬇️ Downloading <b>{title}</b> — 0%")
            result = await library.download_url(info["url"], dest, progress=_progress)
            if not result["ok"]:
                await query.message.edit_text(f"❌ Download failed: {result['reason']}")
                telemetry.emit("downloader", "url_download", "failed", title=title, error=result["reason"][:100])
                return
            rel = dest.relative_to(library.LIBRARY_DIR)
            key = library.register_personal(title, rel, result["size"])
            telemetry.emit("downloader", "url_download", "ok", title=title, bytes=result["size"])

            async def _hunt_subs() -> None:
                res = await asyncio.to_thread(library.ensure_subs, key)
                if res.get("ok") and not res.get("cached") and res.get("lang") not in (None, "none"):
                    try:
                        await _bot.send_message(message.chat.id, f"🌐 Arabic subs found for <b>{title}</b> ({res['lang']}).")
                    except Exception:
                        pass

            asyncio.create_task(_hunt_subs())
            await query.message.edit_text(
                f"✅ <b>{title}</b> added to your library ({result['size'] / 1024**3:.2f} GB).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="▶️ Play now", callback_data=f"plib:{key}"),
                ]]),
            )
        except Exception:
            logger.exception("url download failed")
            await query.message.edit_text("❌ Download crashed — see logs.")
        finally:
            _url_dl_active = False

    async def _offer_download(message: Message, url: str, status: Message | None = None) -> None:
        probe = await library.probe_media_url(url)
        if not probe["ok"]:
            if status:
                await status.edit_text(f"❌ Agent found the link but it's not playable ({probe['reason']}).")
            else:
                await message.answer(f"❌ Agent found the link but it's not playable ({probe['reason']}).")
            return
        from .parsing import guess_title_from_filename
        title, year = guess_title_from_filename(Path(probe["title_source"]).stem)
        if year:
            title = f"{title} ({year})"
        token = uuid.uuid4().hex[:10]
        _URL_DL[token] = {"url": url, "title": title, "size": probe["size"], "chat_id": message.chat.id}
        size = probe["size"]
        size_txt = f"{size / 1024**3:.2f} GB" if size >= 1024**3 else (f"{size / 1024**2:.0f} MB" if size else "unknown size")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬇️ Download", callback_data=f"dlurl:{token}"),
            InlineKeyboardButton(text="✖", callback_data=f"dlurl:no:{token}"),
        ]])
        card = f"⬇️ <b>{title}</b>\n{size_txt} · {probe['content_type']}\nAdd to your library?"
        if status:
            await status.edit_text(f"🕵️ Found it!\n{card}", reply_markup=kb)
        else:
            await message.answer(card, reply_markup=kb)

    @dp.callback_query(F.data.startswith("fetch:"))
    async def cb_fetch(query: CallbackQuery) -> None:
        token = query.data.split(":", 1)[1]
        url = _FETCH_JOBS.pop(token, None)
        if url is None:
            await query.answer("Expired — send the link again.", show_alert=True)
            return
        status = await query.message.edit_text("🕵️ Agent investigating the page…")
        await query.answer()
        from .fetch_agent import run_agent
        result = await run_agent(url)
        if not result["ok"]:
            await status.edit_text(f"🕵️ Agent came back empty-handed: {result['reason']}")
            telemetry.emit("fetcher", "agent", "miss", url=url[:120], reason=result["reason"][:100])
            return
        telemetry.emit("fetcher", "agent", "hit", url=url[:120])
        await _offer_download(query.message, result["url"], status=status)

    @dp.message(Command("fetch"))
    async def cmd_fetch(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().startswith("http"):
            await message.answer("Usage: <code>/fetch https://example.com/video-page</code>")
            return
        url = parts[1].strip()
        status = await message.answer("🕵️ Agent investigating the page…")
        from .fetch_agent import run_agent
        result = await run_agent(url)
        if not result["ok"]:
            await status.edit_text(f"🕵️ Agent came back empty-handed: {result['reason']}")
            return
        await _offer_download(message, result["url"], status=status)

    @dp.callback_query(F.data.startswith("dlurl:"))
    async def cb_dlurl(query: CallbackQuery) -> None:
        parts = query.data.split(":")
        if parts[1] == "no":
            _URL_DL.pop(parts[2], None)
            await query.message.edit_text("Download cancelled.")
            await query.answer()
            return
        await _run_url_download(query, parts[1])

    @dp.message(F.text & ~F.text.startswith("/"))
    async def handle_request(message: Message) -> None:
        url_match = re.search(r"https?://\S+", message.text or "")
        if url_match:
            url = url_match.group(0).rstrip(").,>")
            probe = await library.probe_media_url(url)
            if not probe["ok"] and probe["reason"].startswith("page"):
                token = uuid.uuid4().hex[:10]
                _FETCH_JOBS[token] = url
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🕵️ Agentic fetch", callback_data=f"fetch:{token}"),
                    InlineKeyboardButton(text="✖", callback_data=f"dlurl:no:{token}"),
                ]])
                await message.answer(
                    f"🔗 That's a page link, not a direct media file ({probe['reason']}).\n"
                    "Want me to investigate the page and hunt the video behind it?",
                    reply_markup=kb,
                )
                return
            if not probe["ok"]:
                await message.answer(f"🔗 That link isn't a direct media file ({probe['reason']}).")
                return
            from .parsing import guess_title_from_filename
            title, year = guess_title_from_filename(Path(probe["title_source"]).stem)
            if year:
                title = f"{title} ({year})"
            token = uuid.uuid4().hex[:10]
            _URL_DL[token] = {"url": url, "title": title, "size": probe["size"], "chat_id": message.chat.id}
            size = probe["size"]
            size_txt = f"{size / 1024**3:.2f} GB" if size >= 1024**3 else (f"{size / 1024**2:.0f} MB" if size else "unknown size")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬇️ Download", callback_data=f"dlurl:{token}"),
                InlineKeyboardButton(text="✖", callback_data=f"dlurl:no:{token}"),
            ]])
            await message.answer(
                f"⬇️ <b>{title}</b>\n{size_txt} · {probe['content_type']}\nAdd to your library?",
                reply_markup=kb,
            )
            return
        status = await message.answer("🧠 Thinking…")
        if fastpath.exact_match(message.text):
            logger.info("fast-path exact match %r — skipping LLM", message.text)
            telemetry.emit("router", "fast_path", "hit")
            await run_search(message, status, {"intent": "search", "title": message.text})
            return
        try:
            query = await parse_request(message.text)
        except Exception as exc:
            logger.exception("llm parse failed")
            telemetry.emit("router", "llm_parse", "failed", error=str(exc)[:120])
            if _CONTINUE_RE.search(message.text or ""):
                if _NEXT_RE.search(message.text or ""):
                    if await _send_next_episode(message, None):
                        await status.delete()
                        return
                else:
                    conn = store.connect()
                    has_items = bool(store.continue_watching(conn, message.from_user.id))
                    conn.close()
                    if has_items:
                        await status.delete()
                        await _send_continue_list(message)
                        return
            await status.edit_text(f"❌ Couldn't parse that (LLM error): <code>{exc}</code>{_provider_down_note()}")
            return
        logger.info("parsed %r → %s", message.text, query)
        intent = query.get("intent", "search")
        session = _session(message.chat.id)
        session["resume_pos"] = float(query["start_position"]) if query.get("start_position") else None
        if session["resume_pos"]:
            await message.answer(f"⏱ Got it — will start at {_fmt_time(session['resume_pos'])} once you pick a version and TV.")

        if intent == "recommend":
            answer = query.get("answer")
            titles = query.get("titles") or []
            if answer:
                await message.answer(f"🧠 <i>{answer}</i>")
            if not titles:
                await status.edit_text("🤖 I couldn't map that to a specific title — try naming one, or search directly.")
                return
            first = titles[0]
            await run_search(message, status, {
                "intent": "search", "title": first["title"], "year": first.get("year"),
                "language": None, "subtitles": None, "media_type": None,
            })
            return
        if intent == "resume":
            conn = store.connect()
            has_items = bool(store.continue_watching(conn, message.from_user.id))
            conn.close()
            if has_items:
                await status.delete()
                await _send_continue_list(message)
            else:
                await status.edit_text("Nothing in progress yet — name something to watch!")
            return

        if intent == "next_episode":
            await status.delete()
            if not await _send_next_episode(message, query.get("title")):
                await message.answer("I couldn't tell which series you mean — send <i>continue</i> to see what you were watching.")
            return

        if intent == "status":
            if active_play:
                conn = store.connect()
                what = store.progress_title(conn, active_play.media_key)
                conn.close()
                await status.edit_text(f"📺 Playing on <b>{active_play.tv_name}</b>: <b>{what}</b>")
            else:
                await status.edit_text("Nothing is playing right now. Name a movie or series and I'll cast it!")
            return

        if intent == "feedback":
            await status.edit_text(
                "Sorry about that 🙏 — tell me the <b>title</b> and I'll try again. "
                "You can also send <i>continue</i> to resume, or <i>next episode</i> to skip ahead."
            )
            return

        if intent == "smalltalk":
            await status.edit_text("👋 I'm your media butler — send a movie or series name and I'll cast it to a TV.")
            return

        if query.get("episode") and query.get("media_type") == "series":
            if await _deep_link_episode(message, status, query):
                return

        if not query.get("title"):
            await status.edit_text("Couldn't find a title in that — try naming the movie or series.")
            return
        await run_search(message, status, query)

    async def _deep_link_episode(message: Message, status: Message, query: dict) -> bool:
        words = _title_words(query.get("title") or "")
        conn = store.connect()
        series_id = None
        last_season = None
        for r in conn.execute(
            "SELECT media_key FROM watch_progress WHERE user_id=? AND media_key LIKE 'episode:%' ORDER BY updated_at DESC LIMIT 10",
            (message.from_user.id,),
        ).fetchall():
            _, _, ref = r["media_key"].partition(":")
            ep_id, _, sref = ref.partition(":")
            if not sref:
                continue
            row = conn.execute(
                "SELECT e.season, s.title AS series_title, s.series_id FROM episodes e "
                "JOIN series s ON s.series_id=e.series_id WHERE e.episode_id=?",
                (int(ep_id),),
            ).fetchone()
            if row is None:
                continue
            if words and not (words & _title_words(row["series_title"])):
                continue
            series_id = row["series_id"]
            last_season = row["season"]
            break
        conn.close()

        if series_id is None and words:
            groups = await asyncio.to_thread(search_media, query, MAX_CANDIDATES)
            for group in groups:
                if group.kind == "series" and (_title_words(group.title) & words):
                    series_id = group.variants[0].ref_id
                    break
        if series_id is None:
            return False

        info = await provider.series_info(series_id)
        eps = []
        for season_key, eplist in info.get("episodes", {}).items():
            for e in eplist:
                eps.append((int(season_key), int(e.get("episode_num", 0)), e))
        eps.sort(key=lambda x: (x[0], x[1]))
        wanted = query["episode"]
        candidates = [t for t in eps if t[1] == wanted]
        if not candidates:
            return False
        chosen = next((t for t in candidates if t[0] == last_season), candidates[0])
        s_, n_, e_ = chosen
        ep_name = (e_.get("title") or "").strip()
        srow_conn = store.connect()
        srow = srow_conn.execute("SELECT title FROM series WHERE series_id=?", (series_id,)).fetchone()
        srow_conn.close()
        series_title = srow["title"] if srow else query.get("title")
        label = f"▶ {series_title} — S{s_}E{n_}"
        if ep_name and not ep_name.upper().startswith("S0"):
            label += f" · {ep_name}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=label[:64],
                callback_data=f"ep:{e_['id']}:{e_.get('container_extension', 'mkv')}:{series_id}:{s_}",
            )
        ]])
        await status.edit_text("Found it:", reply_markup=kb)
        return True

    def _library_page(offset: int) -> tuple[str, InlineKeyboardMarkup | None]:
        rows, total = library.list_ready(limit=8, offset=offset)
        if not rows:
            return "📚 Your library is empty.", None
        kb_rows = [
            [InlineKeyboardButton(
                text=f"▶️ {r['title'][:44]} · {r['size_bytes'] / 1024**3:.1f}GB",
                callback_data=f"plib:{r['media_key']}",
            )]
            for r in rows
        ]
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"plibpg:{max(0, offset - 8)}"))
        if offset + 8 < total:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"plibpg:{offset + 8}"))
        if nav:
            kb_rows.append(nav)
        page = offset // 8 + 1
        pages = (total + 7) // 8
        text = f"📚 <b>Library</b> — {total} item(s), page {page}/{pages}\nTap to play on a TV:"
        return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)

    @dp.message(Command("library"))
    async def cmd_library(message: Message) -> None:
        text, kb = _library_page(0)
        await message.answer(text, reply_markup=kb)

    @dp.callback_query(F.data.startswith("plibpg:"))
    async def cb_plibpg(query: CallbackQuery) -> None:
        text, kb = _library_page(int(query.data.split(":")[1]))
        await query.answer()
        if kb is None:
            await query.message.edit_text(text)
            return
        await query.message.edit_text(text, reply_markup=kb)

    @dp.message(Command("live"))
    async def cmd_live(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        status = await message.answer("📡 Loading live channels…")
        provider = Provider()
        try:
            if arg:
                streams = await provider.live_streams()
                q = arg.lower()
                hits = [s for s in streams if q in (s.get("name") or "").lower()][:20]
                if not hits:
                    await status.edit_text(f"📡 No live channel matching “{arg}”.")
                    return
                for s in hits:
                    _LIVE_NAMES[int(s["stream_id"])] = s.get("name") or "?"
                rows = [[InlineKeyboardButton(text=s["name"][:60], callback_data=f"livech:{s['stream_id']}")] for s in hits]
                await status.edit_text(f"📡 Live channels matching “{arg}”:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                return
            cats = await provider.live_categories()
            rows = _live_category_rows(cats, 0)
            await status.edit_text(f"📡 Live TV — {len(cats)} categories:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            logger.exception("live list failed")
            await status.edit_text("📡 Couldn't load live channels (provider error).")

    def _live_category_rows(cats: list, page: int) -> list:
        per = 30
        chunk = cats[page * per:(page + 1) * per]
        rows = [[InlineKeyboardButton(text=(c.get("category_name") or "?")[:60], callback_data=f"livcat:{c['category_id']}:0")] for c in chunk]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"livpage:{page - 1}"))
        if (page + 1) * per < len(cats):
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"livpage:{page + 1}"))
        if nav:
            rows.append(nav)
        return rows

    @dp.callback_query(F.data.startswith("livpage:"))
    async def cb_livpage(query: CallbackQuery) -> None:
        page = int(query.data.split(":")[1])
        cats = await Provider().live_categories()
        try:
            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=_live_category_rows(cats, page)))
        except TelegramBadRequest:
            pass
        await query.answer()

    @dp.callback_query(F.data.startswith("livcat:"))
    async def cb_livcat(query: CallbackQuery) -> None:
        _, cid, page = query.data.split(":")
        page, cid = int(page), int(cid)
        streams = await Provider().live_streams(cid)
        streams.sort(key=lambda s: s.get("num") or 0)
        per = 25
        chunk = streams[page * per:(page + 1) * per]
        for s in chunk:
            _LIVE_NAMES[int(s["stream_id"])] = s.get("name") or "?"
        rows = [[InlineKeyboardButton(text=(s.get("name") or "?")[:60], callback_data=f"livech:{s['stream_id']}")] for s in chunk]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"livcat:{cid}:{page - 1}"))
        if (page + 1) * per < len(streams):
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"livcat:{cid}:{page + 1}"))
        if nav:
            rows.append(nav)
        await query.message.edit_text(f"📡 {len(streams)} channels — page {page + 1}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await query.answer()

    @dp.callback_query(F.data.startswith("livech:"))
    async def cb_livech(query: CallbackQuery) -> None:
        sid = int(query.data.split(":")[1])
        name = _LIVE_NAMES.get(sid, f"Channel {sid}")
        session = _session(query.message.chat.id)
        session["renderers"] = await _discover_merged()
        if not session["renderers"]:
            await query.answer("No TVs online right now.", show_alert=True)
            return
        provider = Provider()
        session["selected"] = None
        session["pending"] = {
            "url": provider.live_url(sid), "title": f"📡 {name}", "kind": "live",
            "ref_id": sid, "media_key": None, "ext": "ts", "mime": "application/x-mpegurl", "live": True,
        }
        await query.message.edit_text(f"📡 <b>{name}</b> — pick a TV:", reply_markup=_tv_keyboard(session))
        await query.answer()

    @dp.message(Command("subsbackfill"))
    async def cmd_subsbackfill(message: Message) -> None:
        global _subs_backfill_running
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else "movies"
        if arg not in ("movies", "tv", "all"):
            arg = "movies"
        if _subs_backfill_running:
            await message.answer("🌐 A subs backfill is already running.")
            return
        conn = store.connect()
        if arg == "tv":
            where = "kind='episode'"
        elif arg == "movies":
            where = "kind IN ('movie','personal')"
        else:
            where = "1=1"
        rows = conn.execute(f"SELECT media_key, title FROM library WHERE status='ready' AND {where}").fetchall()
        conn.close()
        _subs_backfill_running = True
        tally = {"muxed": 0, "sidecar": 0, "embedded": 0, "miss": 0, "skipped": 0}
        status = await message.answer(f"🌐 Subs backfill [{arg}] — {len(rows)} item(s). Already-checked items are skipped (quota-safe).")
        async def _run():
            global _subs_backfill_running
            try:
                for i, r in enumerate(rows, 1):
                    res = await asyncio.to_thread(library.ensure_subs, r["media_key"])
                    if res.get("cached"):
                        tally["skipped"] += 1
                    elif not res.get("ok"):
                        tally["miss"] += 1
                    elif res["lang"].endswith("muxed"):
                        tally["muxed"] += 1
                    elif res["lang"] == "embedded":
                        tally["embedded"] += 1
                    else:
                        tally["sidecar"] += 1
                    if i % 5 == 0:
                        try:
                            await status.edit_text(f"🌐 Backfill [{arg}] {i}/{len(rows)} — ✅{tally['muxed']+tally['sidecar']+tally['embedded']} ⚠️{tally['miss']} ⏭{tally['skipped']}")
                        except Exception:
                            pass
                await status.edit_text(
                    f"🌐 Subs backfill [{arg}] done — {len(rows)} items:\n"
                    f"✅ {tally['muxed']} muxed · {tally['sidecar']} sidecar · {tally['embedded']} already embedded\n"
                    f"⚠️ {tally['miss']} not found · ⏭ {tally['skipped']} previously checked"
                )
            except Exception:
                logger.exception("subs backfill crashed")
            finally:
                _subs_backfill_running = False
        asyncio.create_task(_run())

    @dp.message(Command("scaninbox"))
    async def cmd_scaninbox(message: Message) -> None:
        status = await message.answer("📥 Scanning inbox…")
        result = await asyncio.to_thread(library.scan_inbox)
        for item_key, _t in result.get("imported", []):
            asyncio.create_task(asyncio.to_thread(library.ensure_subs, item_key))
        lines = [f"📥 Inbox scan: <b>{len(result['imported'])}</b> imported, <b>{len(result['skipped'])}</b> skipped"]
        for _, title in result["imported"]:
            lines.append(f"✅ {title}")
        for name, reason in result["skipped"]:
            lines.append(f"⚠️ {name} — {reason}")
        await status.edit_text("\n".join(lines))

    @dp.callback_query(F.data.startswith("plib:"))
    async def cb_plib(query: CallbackQuery) -> None:
        media_key = query.data.split(":", 1)[1]
        conn = store.connect()
        item = store.get_library_item(conn, media_key)
        conn.close()
        if not (item and item["path"] and Path(item["path"]).exists()):
            await query.answer("File missing on disk", show_alert=True)
            return
        session = _session(query.message.chat.id)
        session["renderers"] = await _discover_merged()
        if not session["renderers"]:
            await query.answer("No TVs found — is one on?", show_alert=True)
            return
        session["pending"] = {
            "media_key": media_key,
            "title": item["title"],
            "kind": "library",
            "ext": "mkv",
            "url": "",
            "lang": "lib",
        }
        session["selected"] = None
        await query.message.edit_reply_markup(reply_markup=_tv_keyboard(session))
        await query.answer("Pick a TV")

    @dp.callback_query(F.data.startswith("link:"))
    async def cb_get_link(query: CallbackQuery) -> None:
        session = _session(query.message.chat.id)
        payload = session.get("pending")
        if not payload:
            await query.answer("Expired — search again.", show_alert=True)
            return
        await query.answer("Preparing link…")
        token = session.get("link_token")
        s = proxy.get_session(token) if token else None
        if s is None or s.done:
            try:
                s = await proxy.start_session(
                    payload["url"], payload["title"],
                    mode="cache_downgrade", mime="video/mpeg",
                    start_offset=float(session.get("resume_pos") or 0.0),
                    declare_current=True,
                )
                s.idle_timeout_s = 900.0
            except Exception as exc:
                await query.message.answer(f"⚠️ Couldn't prepare the stream: {str(exc)[:120]}")
                return
            session["link_token"] = s.token
        duration = s.duration_s or 0.0
        if not duration:
            try:
                duration = (await probe_duration(payload["url"])) or 0.0
                s.duration_s = duration
            except Exception:
                pass
        from . import urlsign

        urls = urlsign.urls_for(s.token, proxy.lan_ip_for("192.168.1.1"), ext="ts")
        lines = [f"📱 <b>{payload['title']}</b>", ""]
        if urls.get("public_url"):
            lines += ["<b>Anywhere (public)</b>:", f"<code>{urls['public_url']}</code>", ""]
        lines += [
            "<b>Home network</b>:",
            f"<code>{urls['lan_url']}</code>",
            "",
            "⏱ Valid 6h · one stream at a time · tap a TV above to cast this same stream",
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏹ Stop stream", callback_data=f"stopl:{s.token}")]])
        await query.message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)

    @dp.callback_query(F.data.startswith("stopl:"))
    async def cb_stop_link(query: CallbackQuery) -> None:
        token = query.data.split(":", 1)[1]
        await proxy.stop_session(token)
        session = _session(query.message.chat.id)
        if session.get("link_token") == token:
            session["link_token"] = None
        await query.answer("Stopped")
        try:
            await query.message.edit_text("⏹ Stream stopped — provider slot freed.")
        except TelegramBadRequest:
            pass

    @dp.callback_query(F.data == "rescan")
    async def cb_rescan(query: CallbackQuery) -> None:
        await query.answer("Scanning…")
        renderers = await _discover_merged()
        session = _session(query.message.chat.id)
        session["renderers"] = renderers
        session["selected"] = None
        if not renderers:
            await query.message.edit_text("Still no TVs found.")
            return
        await query.message.edit_reply_markup(reply_markup=_tv_keyboard(session))

    @dp.callback_query(F.data.startswith("tv:"))
    async def cb_pick_tv(query: CallbackQuery) -> None:
        index = int(query.data.split(":")[1])
        session = _session(query.message.chat.id)
        if index >= len(session["renderers"]):
            await query.answer("Stale list — rescan.", show_alert=True)
            return
        session["selected"] = index
        await query.answer(f"Selected {session['renderers'][index].friendly_name}")
        try:
            await query.message.edit_reply_markup(reply_markup=_tv_keyboard(session))
        except TelegramBadRequest:
            pass
        if session.get("pending"):
            result = await _execute_cast(session, query.message.chat.id, query.from_user.id)
            session["pending"] = None
            payload = session.get("last_probe") or {}
            await query.message.answer(result)

    @dp.callback_query(F.data.startswith("go:"))
    async def cb_go(query: CallbackQuery) -> None:
        mode = query.data.split(":")[1]
        session = _session(query.message.chat.id)
        if not session.get("pending"):
            await query.answer("Expired — search again.", show_alert=True)
            return
        if mode == "start":
            session["resume_pos"] = None
        await query.answer("Casting…")
        if await _ensure_tv(query.message, query.message.chat.id):
            result = await _execute_cast(session, query.message.chat.id, query.from_user.id)
            payload = session["pending"]
            session["pending"] = None
            await query.message.answer(result)
            if payload and payload.get("url"):
                asyncio.create_task(_probe_subs(bot, query.message.chat.id, payload["kind"], payload["ref_id"], payload["url"]))

    @dp.callback_query(F.data.startswith("wl:"))
    async def cb_watchlist_add(query: CallbackQuery) -> None:
        gi = int(query.data.split(":")[1])
        session = _session(query.message.chat.id)
        if gi >= len(session["groups"]):
            await query.answer("Stale results.", show_alert=True)
            return
        group = session["groups"][gi]
        conn = store.connect()
        store.add_watchlist(conn, query.from_user.id, group.kind, group.variants[0].ref_id, group.title, group.year, group.poster)
        conn.close()
        await query.answer("📌 Added to watchlist")

    @dp.callback_query(F.data.startswith("wlq:"))
    async def cb_watchlist_query(query: CallbackQuery) -> None:
        _, kind, ref = query.data.split(":")
        conn = store.connect()
        if kind == "movie":
            row = conn.execute("SELECT title, year FROM movies WHERE stream_id=?", (int(ref),)).fetchone()
        else:
            row = conn.execute("SELECT title, year FROM series WHERE series_id=?", (int(ref),)).fetchone()
        conn.close()
        if not row:
            await query.answer("Not in the current catalog — re-index may be needed.", show_alert=True)
            return
        await query.answer()
        status = await query.message.answer("🔎 Searching…")
        await run_search(
            query.message,
            status,
            {"title": row["title"], "year": row["year"], "language": None, "subtitles": None, "media_type": kind},
        )

    @dp.callback_query(F.data.startswith("cgo:"))
    async def cb_continue_go(query: CallbackQuery) -> None:
        media_key = query.data.split(":", 1)[1]
        kind, _, ref = media_key.partition(":")
        conn = store.connect()
        progress = store.get_progress(conn, query.from_user.id, media_key)
        payload = None
        if kind == "movie":
            row = conn.execute("SELECT * FROM movies WHERE stream_id=?", (int(ref),)).fetchone()
            if row:
                payload = {
                    "kind": "movie", "ref_id": row["stream_id"], "media_key": media_key,
                    "title": f"{row['title']} ({row['year'] or ''})".strip(), "lang": row["lang"],
                    "url": provider.movie_url(row["stream_id"], row["ext"]), "ext": row["ext"],
                }
        elif kind == "episode":
            ep_id, _, series_ref = ref.partition(":")
            row = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (int(ep_id),)).fetchone()
            info = None
            if row is None and series_ref:
                info = await provider.series_info(int(series_ref))
                for season_key, eplist in info.get("episodes", {}).items():
                    for e in eplist:
                        conn.execute(
                            "INSERT OR REPLACE INTO episodes(episode_id, series_id, season, episode, title, ext) VALUES(?,?,?,?,?,?)",
                            (int(e["id"]), int(series_ref), int(season_key), int(e.get("episode_num", 0)),
                             (e.get("title") or "").strip(), e.get("container_extension", "mkv")),
                        )
                    conn.commit()
                row = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (int(ep_id),)).fetchone()
            if row:
                srow = conn.execute("SELECT title FROM series WHERE series_id=?", (row["series_id"],)).fetchone()
                series_name = srow["title"] if srow else None
                label = f"S{row['season']}E{row['episode']}"
                payload = {
                    "kind": "episode", "ref_id": row["episode_id"], "media_key": media_key,
                    "title": f"{series_name} — {label}" if series_name else label, "lang": None,
                    "url": provider.episode_url(row["episode_id"], row["ext"]), "ext": row["ext"],
                }
        conn.close()
        if not payload:
            await query.answer("Media vanished from catalog.", show_alert=True)
            return
        await query.answer("Casting…")
        session = _session(query.message.chat.id)
        session["pending"] = payload
        if session.get("resume_pos") is None:
            session["resume_pos"] = progress["position_s"] if progress and progress["position_s"] > 60 else None
        if await _ensure_tv(query.message, query.message.chat.id):
            result = await _execute_cast(session, query.message.chat.id, query.from_user.id)
            session["pending"] = None
            await query.message.answer(result)

    @dp.callback_query(F.data.startswith("sel:"))
    async def cb_select(query: CallbackQuery) -> None:
        _, gi, vi = query.data.split(":")
        gi, vi = int(gi), int(vi)
        session = _session(query.message.chat.id)
        if gi >= len(session["groups"]):
            await query.answer("Stale results — search again.", show_alert=True)
            return
        group = session["groups"][gi]
        await query.answer("…")
        if group.kind == "series":
            series_id = group.variants[vi].ref_id
            try:
                info = await provider.series_info(series_id)
            except Exception as exc:
                await query.message.answer(f"❌ Couldn't load episodes: <code>{exc}</code>{_provider_down_note()}")
                return
            seasons = sorted(int(s) for s in info.get("episodes", {}).keys())
            if not seasons:
                await query.message.answer("No episodes listed for this series.")
                return
            rows = []
            for start in range(0, len(seasons), 4):
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"S{s} ({len(info['episodes'][str(s)])} ep)",
                            callback_data=f"seas:{gi}:{series_id}:{s}",
                        )
                        for s in seasons[start : start + 4]
                    ]
                )
            await query.message.answer(
                f"📚 <b>{group.title}</b> — {len(seasons)} season(s)",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return
        variant = group.variants[vi]
        payload = _build_payload(group, variant)
        if await _offer_resume(query.message, session, payload, query.from_user.id):
            return
        session["pending"] = payload
        session["resume_pos"] = None
        if await _ensure_tv(query.message, query.message.chat.id):
            result = await _execute_cast(session, query.message.chat.id, query.from_user.id)
            session["pending"] = None
            await query.message.answer(result)
            asyncio.create_task(_probe_subs(bot, query.message.chat.id, payload["kind"], payload["ref_id"], payload["url"]))

    @dp.callback_query(F.data.startswith("seas:"))
    async def cb_season(query: CallbackQuery) -> None:
        _, gi, series_id, season = query.data.split(":")
        gi, season = int(gi), int(season)
        session = _session(query.message.chat.id)
        group = session["groups"][gi] if gi < len(session["groups"]) else None
        info = await provider.series_info(int(series_id))
        episodes = sorted(info.get("episodes", {}).get(str(season), []), key=lambda e: int(e.get("episode_num", 0)))
        conn = store.connect()
        conn.executemany(
            "INSERT OR REPLACE INTO episodes(episode_id, series_id, season, episode, title, ext) VALUES(?,?,?,?,?,?)",
            [(int(e["id"]), int(series_id), int(season), int(e.get("episode_num", 0)),
              (e.get("title") or "").strip(), e.get("container_extension", "mkv")) for e in episodes],
        )
        conn.commit()
        conn.close()
        rows = []
        chunk = []
        shown = 0
        for ep in episodes:
            if shown >= MAX_EPISODE_BUTTONS:
                break
            ext = ep.get("container_extension", "mkv")
            num = ep.get("episode_num", "?")
            chunk.append(InlineKeyboardButton(text=f"E{num}", callback_data=f"ep:{ep['id']}:{ext}:{series_id}:{season}"))
            shown += 1
            if len(chunk) == 6:
                rows.append(chunk)
                chunk = []
        if chunk:
            rows.append(chunk)
        total = len(episodes)
        note = f"showing first {MAX_EPISODE_BUTTONS}" if total > MAX_EPISODE_BUTTONS else "all"
        title = group.title if group else f"series {series_id}"
        await query.message.answer(
            f"📚 <b>{title}</b> — Season {season} ({total} eps, {note})",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("nxt:"))
    async def cb_next_episode(query: CallbackQuery) -> None:
        _, series_id_s, ep_id_s = query.data.split(":")
        series_id, episode_id = int(series_id_s), int(ep_id_s)
        nxt = _next_episode_row(series_id, episode_id)
        if nxt is None:
            await query.answer("No next episode found", show_alert=True)
            return
        conn = store.connect()
        erow = conn.execute("SELECT ext, season, episode, title FROM episodes WHERE episode_id=?", (nxt["episode_id"],)).fetchone()
        srow = conn.execute("SELECT title FROM series WHERE series_id=?", (series_id,)).fetchone()
        conn.close()
        ext = (erow["ext"] if erow and erow["ext"] else "mkv")
        series_title = srow["title"] if srow else f"series {series_id}"
        label = f"{series_title} — S{nxt['season']}E{nxt['episode']}"
        if erow and (erow["title"] or "").strip():
            label += f" · {erow['title'].strip()}"
        session = _session(query.message.chat.id)
        payload = {
            "kind": "episode",
            "ref_id": nxt["episode_id"],
            "media_key": f"episode:{nxt['episode_id']}:{series_id}",
            "title": label,
            "lang": None,
            "url": provider.episode_url(nxt["episode_id"], ext),
            "ext": ext,
        }
        await query.answer(f"▶️ {label}")
        last_udn = session.get("last_tv_udn")
        target = None
        if last_udn:
            try:
                renderers = await _discover_merged()
                target = next((r for r in renderers if r.udn == last_udn), None)
            except Exception:
                logger.exception("next-episode TV rediscovery failed")
        if target is None:
            session["pending"] = payload
            session["resume_pos"] = None
            if await _ensure_tv(query.message, query.message.chat.id):
                result = await _execute_cast(session, query.message.chat.id, query.from_user.id)
                session["pending"] = None
                await query.message.answer(result)
            return
        session["renderers"] = [target]
        session["selected"] = 0
        session["pending"] = payload
        session["resume_pos"] = None
        result = await _execute_cast(session, query.message.chat.id, query.from_user.id)
        session["pending"] = None
        await query.message.answer(result)

    @dp.callback_query(F.data.startswith("ep:"))
    async def cb_episode(query: CallbackQuery) -> None:
        parts = query.data.split(":")
        _, episode_id, ext = parts[0], int(parts[1]), parts[2]
        episode_label = f"Episode {episode_id}"
        if len(parts) >= 5:
            series_id, season = int(parts[3]), int(parts[4])
            try:
                info = await provider.series_info(series_id)
                eps = info.get("episodes", {}).get(str(season), [])
                ep = next((e for e in eps if int(e.get("id", 0)) == episode_id), None)
                if ep is not None:
                    conn = store.connect()
                    srow = conn.execute("SELECT title FROM series WHERE series_id=?", (series_id,)).fetchone()
                    conn.close()
                    series_title = srow["title"] if srow else f"series {series_id}"
                    ep_name = (ep.get("title") or "").strip()
                    episode_label = f"{series_title} — S{season}E{ep.get('episode_num', '?')}"
                    if ep_name:
                        episode_label += f" · {ep_name}"
            except Exception:
                logger.exception("episode label lookup failed")
        session = _session(query.message.chat.id)
        payload = {
            "kind": "episode",
            "ref_id": episode_id,
            "media_key": f"episode:{episode_id}:{series_id}" if len(parts) >= 5 else f"episode:{episode_id}",
            "title": episode_label,
            "lang": None,
            "url": provider.episode_url(episode_id, ext),
            "ext": ext,
        }
        await query.answer()
        if await _offer_resume(query.message, session, payload, query.from_user.id):
            return
        session["pending"] = payload
        session["resume_pos"] = None
        if await _ensure_tv(query.message, query.message.chat.id):
            result = await _execute_cast(session, query.message.chat.id, query.from_user.id)
            session["pending"] = None
            await query.message.answer(result)
            asyncio.create_task(_probe_subs(bot, query.message.chat.id, "episode", episode_id, payload["url"]))

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(
        [
            BotCommand(command="tvs", description="Find and pick a TV"),
            BotCommand(command="bantv", description="Ban a TV by MAC (hide it everywhere)"),
            BotCommand(command="unbantv", description="Unban a TV by MAC"),
            BotCommand(command="audio", description="List or switch audio tracks"),
            BotCommand(command="continue", description="Continue watching"),
            BotCommand(command="watchlist", description="Saved titles"),
            BotCommand(command="stop", description="Stop playback"),
            BotCommand(command="ping", description="Alive check"),
            BotCommand(command="audit", description="Self-audit now"),
            BotCommand(command="live", description="Live TV channels"),
            BotCommand(command="subsbackfill", description="Fetch Arabic subs (movies/tv/all)"),
            BotCommand(command="fetch", description="Hunt the video behind a page link"),
            BotCommand(command="token", description="Create an app access token"),
            BotCommand(command="tokens", description="Manage app tokens"),
        ]
    )
    asyncio.create_task(warm())
    asyncio.create_task(proxy.start_server())
    from .web import WEB_PORT, start_in_thread

    start_in_thread()
    logger.info("Web panel on :%d", WEB_PORT)
    asyncio.create_task(_library_worker())
    asyncio.create_task(_audit_worker())
    conn = store.connect()
    requeued = conn.execute(
        "UPDATE download_queue SET status='queued', error='requeued after restart' WHERE status='downloading'"
    ).rowcount
    conn.commit()
    conn.close()
    if requeued:
        logger.warning("requeued %d download(s) orphaned by restart", requeued)

    asyncio.create_task(_provider_health_watch())

    def _group_json(g: Group) -> dict:
        rating = max((v.rating or 0) for v in g.variants) if g.variants else 0
        return {
            "kind": g.kind, "title": g.title, "year": g.year,
            "score": round(g.best_score, 2), "rating": round(float(rating), 1),
            "poster": g.poster, "plot": g.plot,
            "variants": [
                {"media_key": ("movie:" if g.kind == "movie" else "series:") + str(v.ref_id),
                 "lang": v.lang, "ext": v.ext}
                for v in g.variants[:8]
            ],
        }

    def _filter_groups(groups, year_from, year_to, rating_min, lang):
        out = []
        for g in groups:
            if year_from and (g.year or 0) < year_from:
                continue
            if year_to and (g.year or 0) > year_to:
                continue
            if rating_min and (max((v.rating or 0) for v in g.variants) if g.variants else 0) < rating_min:
                continue
            if lang and not any((v.lang or "").upper() == lang.upper() for v in g.variants):
                continue
            out.append(g)
        return out

    async def _discover_fast(q: str, kind: str = "", year_from: int = 0, year_to: int = 0,
                             rating_min: float = 0.0, lang: str = "", genre: str = ""):
        key = json.dumps([q.lower().strip(), kind, year_from, year_to, rating_min, lang, genre])
        now = time.time()
        cached = _DISCOVER_CACHE.get(key)
        if cached and now - cached[0] < 600:
            return key, cached[1]

        groups = await asyncio.to_thread(
            search_media, {"title": q, "media_type": kind or None, "language": lang or None}, 12
        )
        groups = _filter_groups(groups, year_from, year_to, rating_min, lang)
        keyword_groups = await asyncio.to_thread(
            keyword_search, q, kind or None, 12, year_from, year_to, rating_min, lang, genre
        )

        merged: list[Group] = []
        seen: set = set()
        for g in list(groups) + list(keyword_groups):
            k = (g.kind, normalize_for_grouping(g.title), g.year)
            if k in seen:
                continue
            seen.add(k)
            merged.append(g)
        pick_keys = {(g.kind, normalize_for_grouping(g.title), g.year) for g in merged[:1]}
        data = {
            "ok": True, "query": q, "ai_pending": bool(merged), "ai_note": "", "ai_picks": [],
            "by_name": [_group_json(g) for g in keyword_groups],
            "by_meaning": [_group_json(g) for g in groups],
        }
        _DISCOVER_CACHE[key] = (now, data)
        return key, data

    async def api_discover(q: str, kind: str = "", year_from: int = 0, year_to: int = 0,
                           rating_min: float = 0.0, lang: str = "", genre: str = "") -> dict:
        query = (q or "").strip()
        if not query:
            return {"ok": False, "error": "query required", "ai_picks": [], "by_name": [], "by_meaning": []}
        _, data = await _discover_fast(query, kind, year_from, year_to, rating_min, lang, genre)
        return data

    async def api_discover_ai(q: str, kind: str = "", year_from: int = 0, year_to: int = 0,
                              rating_min: float = 0.0, lang: str = "", genre: str = "") -> dict:
        query = (q or "").strip()
        if not query:
            return {"ok": False, "error": "query required", "ai_picks": [], "ai_note": ""}
        key, data = await _discover_fast(query, kind, year_from, year_to, rating_min, lang, genre)
        now = time.time()
        cached = _DISCOVER_AI_CACHE.get(key)
        if cached and now - cached[0] < 600:
            return cached[1]

        pool = list(data.get("by_meaning") or []) + list(data.get("by_name") or [])
        candidates: list[dict] = []
        seen: set = set()
        for item in pool:
            k = (item["kind"], normalize_for_grouping(item["title"]), item["year"])
            if k in seen:
                continue
            seen.add(k)
            candidates.append({
                "title": item["title"], "year": item["year"], "kind": item["kind"],
                "rating": item.get("rating") or 0, "plot": (item.get("plot") or "")[:140],
            })
        candidates = candidates[:12]
        llm_result = await llm_suggest(query, candidates) if candidates else None
        note = ""
        picks: list[dict] = []
        if llm_result:
            note = str(llm_result.get("note") or "").strip()
            for pick in (llm_result.get("picks") or [])[:5]:
                if not isinstance(pick, dict):
                    continue
                title = str(pick.get("title") or "").strip()
                if not title:
                    continue
                low = re.sub(r"\s*\(\d{4}\)\s*$", "", title).lower()
                match = next((x for x in pool if x["title"].lower() == low), None)
                if match is None:
                    match = next(
                        (x for x in pool if low in x["title"].lower() or x["title"].lower() in low), None
                    )
                if match is None:
                    continue
                item = dict(match)
                item["reason"] = str(pick.get("reason") or "").strip()[:160]
                picks.append(item)
        if not picks:
            ranked = sorted(pool, key=lambda x: (x.get("rating") or 0, x.get("score") or 0), reverse=True)[:6]
            picks = [{**x, "reason": "Top-rated match"} for x in ranked]
            note = note or "AI picks unavailable — showing top-rated matches."
        result = {"ok": True, "query": query, "ai_note": note, "ai_picks": picks}
        _DISCOVER_AI_CACHE[key] = (now, result)
        return result

    async def api_similar(media_key: str) -> dict:
        payload = await _resolve_media_key(media_key)
        if not payload:
            return {"ok": False, "error": "unknown media key", "items": []}
        title = payload.get("title") or ""
        groups = await asyncio.to_thread(
            search_media, {"title": title, "media_type": payload.get("kind")}, 12
        )
        target = normalize_for_grouping(title)
        items = [_group_json(g) for g in groups if normalize_for_grouping(g.title) != target][:10]
        return {"ok": True, "title": title, "items": items}

    async def api_continue() -> dict:
        conn = store.connect()
        try:
            rows = store.continue_watching(conn, library.owner_chat_id() or 0, 12)
            out = []
            for r in rows:
                key = r["media_key"]
                kind, _, ref = key.partition(":")
                title = poster = None
                year = None
                if kind == "movie" and ref.isdigit():
                    m = conn.execute(
                        "SELECT title, year, poster FROM movies WHERE stream_id=?", (int(ref),)
                    ).fetchone()
                    if m:
                        title, year, poster = m["title"], m["year"], m["poster"]
                elif kind == "episode":
                    ep, _, sid = ref.partition(":")
                    if sid.isdigit():
                        s = conn.execute(
                            "SELECT title, poster FROM series WHERE series_id=?", (int(sid),)
                        ).fetchone()
                        if s:
                            title, poster = s["title"], s["poster"]
                    e = conn.execute(
                        "SELECT season, episode FROM episodes WHERE episode_id=?", (int(ep),)
                    ).fetchone() if ep.isdigit() else None
                    if e:
                        title = f"{title or 'Episode'} S{e['season']}E{e['episode']}"
                if not title:
                    continue
                out.append({
                    "media_key": key, "kind": kind, "title": title, "poster": poster, "year": year,
                    "position": round(r["position_s"], 1), "duration": round(r["duration_s"], 1),
                    "updated_at": r["updated_at"],
                })
            return {"ok": True, "items": out}
        finally:
            conn.close()

    async def api_progress(media_key: str) -> dict:
        conn = store.connect()
        row = store.get_progress(conn, library.owner_chat_id() or 0, media_key)
        conn.close()
        return {
            "ok": True,
            "position": round(row["position_s"], 1) if row else 0.0,
            "duration": round(row["duration_s"], 1) if row else 0.0,
        }

    async def api_set_progress(media_key: str, position: float, duration: float = 0.0) -> dict:
        conn = store.connect()
        store.set_progress(conn, library.owner_chat_id() or 0, media_key, float(position), float(duration))
        conn.close()
        return {"ok": True}

    async def api_health() -> dict:
        conn = store.connect()
        lib_bytes = store.library_size_bytes(conn)
        ev = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        conn.close()
        return {
            "ok": True, "provider": provider_health(), "sessions": len(proxy._sessions),
            "library_bytes": lib_bytes, "cache_bytes": library.cache_usage_bytes(),
            "events": ev, "uptime_s": int(time.time() - _STARTED_AT),
        }

    async def api_events(since: int = 0) -> dict:
        conn = store.connect()
        rows = store.list_events(conn, int(since), 50)
        conn.close()
        return {"ok": True, "items": [dict(r) for r in rows]}

    async def api_client_log(level: str, tag: str, message: str) -> dict:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": (level or "I")[:8],
            "tag": (tag or "")[:40], "message": (message or "")[:2000],
        }
        try:
            Path("data/client_logs.jsonl").parent.mkdir(parents=True, exist_ok=True)
            with open("data/client_logs.jsonl", "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            logger.debug("client log write failed", exc_info=True)
        return {"ok": True}

    async def api_tokens_create(label: str = "") -> dict:
        import secrets
        token = secrets.token_urlsafe(24)
        conn = store.connect()
        store.create_app_token(conn, token, label)
        conn.close()
        return {"ok": True, "token": token, "label": label}

    async def api_tokens_list() -> dict:
        conn = store.connect()
        rows = store.list_app_tokens(conn)
        conn.close()
        return {"ok": True, "items": [dict(r) for r in rows]}

    async def api_tokens_revoke(token: str) -> dict:
        conn = store.connect()
        store.revoke_app_token(conn, token)
        conn.close()
        return {"ok": True}

    async def api_pair_notify(request_id: str) -> dict:
        conn = store.connect()
        try:
            row = store.get_pair_request(conn, request_id)
        finally:
            conn.close()
        if row is None:
            return {"ok": False, "error": "unknown request"}
        owner = library.owner_chat_id()
        if not owner:
            return {"ok": False, "error": "no owner"}
        text = (
            "📱 <b>New device wants access</b>\n\n"
            f"<b>{row['device_name'] or 'Unknown device'}</b>\n"
            f"{row['platform'] or 'unknown platform'}\n"
            f"IP: <code>{row['ip'] or '—'}</code>\n"
            f"Code: <code>{row['code']}</code>\n\n"
            "Approve only if this code is shown in the app requesting access."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"pair:ok:{request_id}"),
                    InlineKeyboardButton(text="❌ Deny", callback_data=f"pair:no:{request_id}"),
                ]
            ]
        )
        await _bot.send_message(owner, text, reply_markup=kb)
        return {"ok": True}

    async def api_search(q: str, kind: str) -> list:
        if kind == "live":
            streams = await provider.live_streams()
            ql = q.lower().strip()
            hits = [s for s in streams if ql in (s.get("name") or "").lower()] if ql else streams[:50]
            return [{"kind": "live", "channel_id": s["stream_id"], "title": s.get("name")} for s in hits[:40]]
        groups = await asyncio.to_thread(search_media, {"title": q, "media_type": kind or None}, 8)
        out = []
        for g in groups:
            out.append({
                "kind": g.kind, "title": g.title, "year": g.year, "score": round(g.best_score, 2),
                "poster": g.poster, "plot": g.plot,
                "variants": [
                    {"media_key": f"movie:{v.ref_id}" if g.kind == "movie" else f"series:{v.ref_id}",
                     "lang": v.lang, "ext": v.ext}
                    for v in g.variants[:8]
                ],
            })
        return out

    async def api_episodes(series_id: int) -> dict:
        info = await provider.series_info(series_id)
        raw = info.get("episodes", {}) or {}
        items = {}
        for season_key, eplist in raw.items():
            items[str(season_key)] = [
                {
                    "episode": int(e.get("episode_num", 0)),
                    "title": (e.get("title") or "").strip() or None,
                    "media_key": f"episode:{e.get('id')}:{series_id}",
                    "ext": e.get("container_extension"),
                    "duration": (e.get("info") or {}).get("duration"),
                }
                for e in eplist if e.get("id") is not None
            ]
        return {"episodes": raw, "items": items, "series_id": series_id}

    async def api_tvs() -> list:
        conn = store.connect()
        rows = conn.execute("SELECT name, ip, mac, kind, prefer_proxy, downgrade, ignored FROM tvs WHERE ignored=0").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    async def api_ban_tv(mac: str = "", label: str = "") -> dict:
        conn = store.connect()
        try:
            identity = store.normalize_mac(mac)
            if not identity and label:
                row = store.find_tv_by_name(conn, label)
                identity = store.normalize_mac(row["mac"]) if row and row["mac"] else None
            if not identity:
                return {"ok": False, "error": "MAC address required"}
            store.ban_tv(conn, identity, label)
        finally:
            conn.close()
        return {"ok": True, "mac": identity}

    async def api_unban_tv(mac: str = "") -> dict:
        if not store.normalize_mac(mac):
            return {"ok": False, "error": "MAC address required"}
        conn = store.connect()
        try:
            store.unban_tv(conn, mac)
        finally:
            conn.close()
        return {"ok": True, "mac": store.normalize_mac(mac)}

    async def api_banned_tvs() -> dict:
        conn = store.connect()
        try:
            banned = [dict(r) for r in store.list_banned(conn)]
            rows = conn.execute("SELECT name, ip, mac FROM tvs WHERE ignored=1").fetchall()
        finally:
            conn.close()
        return {
            "ok": True,
            "banned": banned,
            "macs": sorted({b["mac"] for b in banned} | {r["mac"] for r in rows if r["mac"]}),
            "ips": sorted({r["ip"] for r in rows if r["ip"]}),
            "names": sorted({r["name"] for r in rows}),
        }

    async def _resolve_renderer(tv_name: str):
        from .ssdp import Renderer, discover_renderers
        if tv_name:
            conn = store.connect()
            cast_row = conn.execute(
                "SELECT * FROM tvs WHERE kind='cast' AND ignored=0 AND name LIKE ? ORDER BY LENGTH(name) LIMIT 1",
                (f"%{tv_name}%",),
            ).fetchone()
            conn.close()
            if cast_row is not None:
                ip = cast_row["ip"]
                alive = False
                if ip:
                    try:
                        _r, w = await asyncio.wait_for(asyncio.open_connection(ip, 8009), timeout=2)
                        w.close()
                        alive = True
                    except Exception:
                        alive = False
                if not alive:
                    moved = await _ip_for_mac(cast_row["mac"])
                    if moved:
                        try:
                            _r, w = await asyncio.wait_for(asyncio.open_connection(moved, 8009), timeout=2)
                            w.close()
                            ip = moved
                            alive = True
                        except Exception:
                            alive = False
                if alive:
                    try:
                        conn = store.connect()
                        store.upsert_tv(conn, cast_row["udn"], cast_row["name"], ip,
                                        cast_row["location"], mac=cast_row["mac"], kind="cast")
                        conn.close()
                    except Exception:
                        pass
                    logger.info("api resolve: cast TV %r at %s", cast_row["name"], ip)
                    return CastTarget(friendly_name=cast_row["name"], udn=cast_row["udn"], ip=ip)
                logger.warning("api resolve: cast TV %r is off/unreachable (port 8009)", cast_row["name"])
                return None
        discovered = await discover_renderers()
        conn = store.connect()
        rows = conn.execute("SELECT name, ip, location, mac, udn FROM tvs WHERE ignored=0").fetchall()
        conn.close()

        def match_row(ip: str, udn: str):
            mac = store.arp_mac(ip)
            for row in rows:
                if row["udn"] and udn and row["udn"] == udn:
                    return row
                if row["mac"] and mac and row["mac"].lower() == mac.lower():
                    return row
                if row["ip"] == ip:
                    return row
            return None

        candidates = []
        for r in discovered:
            ip = r.location.split("//")[-1].split(":")[0]
            row = match_row(ip, r.udn)
            if row is None:
                continue
            candidates.append(((row["name"] or r.friendly_name), r, row, ip))
        if tv_name:
            named = [c for c in candidates if tv_name.lower() in c[0].lower()]
            if not named:
                logger.warning("requested TV %r not found among live renderers — refusing cross-TV fallback", tv_name)
                return None
            candidates = named
        if not candidates and tv_name:
            row = next((r for r in rows if tv_name.lower() in (r["name"] or "").lower()), None)
            if row and row["mac"]:
                try:
                    import subprocess as _sp
                    out = await asyncio.to_thread(lambda: _sp.run(["ip", "-4", "neigh"], capture_output=True, text=True, timeout=5).stdout)
                    mac = (row["mac"] or "").lower()
                    cur_ip = None
                    for line in out.splitlines():
                        parts = line.split()
                        if len(parts) >= 4 and store.arp_mac(parts[0]) and store.arp_mac(parts[0]).lower() == mac:
                            cur_ip = parts[0]
                            break
                    if cur_ip:
                        m = re.match(r"(https?://)[^/]+(/.*)", row["location"] or "")
                        loc2 = (m.group(1) + cur_ip + m.group(2)) if m else (row["location"] or f"http://{cur_ip}:2870/dmr.xml")
                        import aiohttp as _aio
                        async with _aio.ClientSession() as sess:
                            async with sess.get(loc2, timeout=_aio.ClientTimeout(total=6)) as resp:
                                ok_desc = resp.status == 200
                        if ok_desc:
                            logger.warning("api resolve: SSDP silent — DB+ARP fallback for %s at %s", tv_name, cur_ip)
                            try:
                                conn = store.connect()
                                store.upsert_tv(conn, row["udn"], row["name"], cur_ip, loc2, mac=mac)
                                conn.close()
                            except Exception:
                                pass
                            return Renderer(location=loc2, friendly_name=row["name"], udn=row["udn"])
                except Exception:
                    logger.exception("DB+ARP renderer fallback failed")
            logger.warning("api resolve: no discovered TV matched (tv_name=%r, discovered=%d)", tv_name, len(discovered))
            return None
        name, r, row, ip = candidates[0]
        try:
            conn = store.connect()
            store.upsert_tv(conn, r.udn, name, ip, r.location, mac=store.arp_mac(ip))
            conn.close()
        except Exception:
            pass
        return Renderer(location=r.location, friendly_name=name, udn=r.udn)

    async def api_play(media_key: str, tv_name: str, start_position: float,
                       audio: int = -1, sub: int = -1) -> dict:
        payload = await _resolve_media_key(media_key)
        if not payload:
            return {"ok": False, "error": f"media_key {media_key} not found"}
        renderer = await _resolve_renderer(tv_name)
        if renderer is None:
            return {"ok": False, "error": "no TV found (check name or power state)"}
        session = _session(0)
        session["pending"] = payload
        session["resume_pos"] = float(start_position) if start_position else None
        session["audio_index"] = int(audio) if int(audio) >= 0 else None
        session["sub_index"] = int(sub) if int(sub) >= 0 else None
        session["renderers"] = [renderer]
        session["selected"] = 0
        try:
            result = await _execute_cast(session, library.owner_chat_id() or 0, 0)
        except Exception as exc:
            logger.exception("api play failed")
            result = f"{type(exc).__name__}: {str(exc)[:160]}"
        session["pending"] = None
        return {"ok": "PLAYING" in result or "Started at" in result, "detail": result}

    async def api_live_play(channel_id: int, tv_name: str) -> dict:
        streams = await provider.live_streams()
        ch = next((c for c in streams if int(c.get("stream_id", 0)) == channel_id), None)
        if ch is None:
            return {"ok": False, "error": "channel not found"}
        payload = {
            "kind": "live", "ref_id": channel_id, "media_key": f"live:{channel_id}",
            "title": ch.get("name") or f"live {channel_id}", "url": provider.live_url(channel_id),
            "live": True, "ext": "ts",
        }
        renderer = await _resolve_renderer(tv_name)
        if renderer is None:
            return {"ok": False, "error": "no TV found"}
        session = _session(0)
        session["pending"] = payload
        session["resume_pos"] = None
        session["renderers"] = [renderer]
        session["selected"] = 0
        try:
            result = await _execute_cast(session, library.owner_chat_id() or 0, 0)
        except Exception as exc:
            logger.exception("api live play failed")
            result = f"{type(exc).__name__}: {str(exc)[:160]}"
        session["pending"] = None
        return {"ok": "PLAYING" in result, "detail": result}

    async def api_control(action: str, value: float) -> dict:
        if active_play is None:
            return {"ok": False, "error": "nothing playing"}
        target = active_play.renderer
        try:
            return await _api_control_inner(action, value, target)
        except Exception as exc:
            logger.exception("api control %s failed", action)
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    async def _api_control_inner(action: str, value: float, target):
        if action == "pause":
            if isinstance(target, CastTarget):
                from .cc import pause as cc_pause
                ok = cc_pause(target.ip)
            else:
                from .caster import pause as dlna_pause
                ok = await dlna_pause(target)
            final = None
            for _ in range(5):
                await asyncio.sleep(1.5)
                try:
                    final = await playback_state(target)
                except Exception:
                    final = None
                if final in ("PAUSED_PLAYBACK", "PAUSED"):
                    active_play.paused = True
                    return {"ok": True, "state": "paused"}
                if final == "STOPPED":
                    active_play.paused = False
                    return {"ok": False, "state": "stopped"}
            active_play.paused = bool(ok)
            return {"ok": bool(ok), "state": "paused" if ok else "unknown"}
        if action == "resume":
            if isinstance(target, CastTarget):
                from .cc import resume as cc_resume
                ok = cc_resume(target.ip)
            else:
                from .caster import resume as dlna_resume
                ok = await dlna_resume(target)
            final = None
            for _ in range(5):
                await asyncio.sleep(1.5)
                try:
                    final = await playback_state(target)
                except Exception:
                    final = None
                if final == "PLAYING":
                    active_play.paused = False
                    return {"ok": True, "state": "playing"}
            active_play.paused = not bool(ok)
            return {"ok": bool(ok), "state": "playing" if ok else "unknown"}
        if action == "seek":
            merged = None
            if active_play.proxy_token:
                s = proxy.get_session(active_play.proxy_token)
                merged = proxy.progress_seconds(s) if s else None
            pd = await _active_position()
            base = (merged or pd or (0.0, 0.0))[0]
            new_pos = max(0.0, base + value)
            if active_play.proxy_token and not isinstance(target, CastTarget):
                ok = await _server_side_seek(new_pos, target)
            elif isinstance(target, CastTarget):
                from .cc import seek as cc_seek
                ok = cc_seek(target.ip, new_pos)
            else:
                ok = await seek(target, new_pos)
            return {"ok": bool(ok), "position": new_pos}
        if action == "stop":
            await _stop_active("stopped via api")
            return {"ok": True, "state": "stopped"}
        return {"ok": False, "error": f"unknown action {action}"}

    async def api_status() -> dict:
        if active_play is None:
            return {"playing": False}
        pos = None
        diag = None
        if active_play.proxy_token:
            s = proxy.get_session(active_play.proxy_token)
            merged = proxy.progress_seconds(s) if s else None
            if merged:
                pos = {"position": round(merged[0], 1), "duration": round(merged[1], 1)}
            if s:
                diag = {"token": s.token, "start_offset": s.start_offset, "duration_s": round(s.duration_s),
                        "total": s.total, "max_served": s.max_served, "is_live": s.is_live}
        if pos is None:
            pd = await _active_position()
            if pd:
                pos = {"position": round(pd[0], 1), "duration": round(pd[1], 1)}
        return {"playing": True, "tv": active_play.tv_name, "paused": active_play.paused,
                "title": None, "position": pos, "diag": diag}

    async def api_test_virtual(tv_name: str, file_path: str = "", start_position: float = 0.0, seek_to: float = -1.0, transcode: bool = True, loop: bool = False) -> dict:
        global active_play
        if seek_to >= 0:
            if active_play is None or not active_play.proxy_token:
                return {"ok": False, "error": "nothing playing"}
            ok, detail = await proxy.seek_session(active_play.proxy_token, float(seek_to))
            await asyncio.sleep(5)
            s = proxy.get_session(active_play.proxy_token)
            pos = proxy.progress_seconds(s) if s else None
            return {"ok": bool(ok), "detail": detail, "position": pos[0] if pos else None}
        path = Path(file_path) if file_path else None
        if path is None or not path.exists():
            cands = sorted(library.LIBRARY_DIR.rglob("*.mkv")) + sorted(library.LIBRARY_DIR.rglob("*.mp4"))
            if not cands:
                return {"ok": False, "error": "no library files found"}
            path = cands[0]
        renderer = await _resolve_renderer(tv_name)
        if renderer is None:
            return {"ok": False, "error": "no TV found (check name or power state)"}
        tv_ip = renderer.location.split("//")[-1].split(":")[0]
        loop_t = asyncio.get_running_loop().time
        try:
            await stop(renderer)
            for _ in range(8):
                if await playback_state(renderer) == "STOPPED":
                    break
                await asyncio.sleep(1)
        except Exception:
            pass
        session_obj = await proxy.start_virtual_session(str(path), f"Library test: {path.name}", transcode=transcode, start_offset=float(start_position or 0.0), loop=loop)
        try:
            state = await cast(renderer, proxy.proxied_url(session_obj.token, tv_ip), f"Library test: {path.name}", "ts", protocol_info=_protocol_info_for(renderer.friendly_name, "ts"))
            kick_wait = loop_t() + 12
            while loop_t() < kick_wait and session_obj.max_served == 0:
                await asyncio.sleep(1)
            if session_obj.max_served == 0:
                logger.warning("test virtual: no TV contact in 12s — single kick-cast")
                try:
                    state = await cast(renderer, proxy.proxied_url(session_obj.token, tv_ip), f"Library test: {path.name}", "ts", protocol_info=_protocol_info_for(renderer.friendly_name, "ts"))
                except Exception:
                    pass
            waits = 0
            while state == "TRANSITIONING" and waits < 10:
                await asyncio.sleep(5)
                waits += 1
                try:
                    state = await playback_state(renderer)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("test virtual cast raised %s — running bytes-truth window", exc)
            state = "STOPPED"
        if state != "PLAYING":
            served_a = session_obj.max_served
            deadline = loop_t() + 75
            while loop_t() < deadline:
                await asyncio.sleep(3)
                recent = (loop_t() - session_obj.last_client_seen) < 15
                if session_obj.max_served > served_a and recent:
                    state = "PLAYING"
                    logger.warning("test virtual: control plane lied but TV consumed bytes (%d->%d) — playing", served_a, session_obj.max_served)
                    break
            if state != "PLAYING":
                await proxy.stop_session(session_obj.token)
                return {"ok": False, "error": "TV made contact but never consumed bytes in 75s", "token": session_obj.token}
        conn = store.connect()
        play_id = store.start_play(conn, 0, "movie", 0, None, f"Library test: {path.name}", None, renderer.friendly_name)
        conn.close()
        task = asyncio.create_task(_poll_playback_dlna(play_id, renderer, 0, None, session_obj.token))
        active_play = ActivePlay(play_id=play_id, renderer=renderer, tv_name=renderer.friendly_name, media_key=None, task=task, proxy_token=session_obj.token)
        served = session_obj.max_served
        return {"ok": state == "PLAYING" or served > 0, "state": state, "token": session_obj.token, "file": path.name, "tv_bytes": served}

    async def api_library_stream(media_key: str) -> dict:
        from . import urlsign

        conn = store.connect()
        item = store.get_library_item(conn, media_key)
        conn.close()
        if item is None or not item["path"]:
            return {"ok": False, "error": "not in library"}
        path = Path(item["path"])
        if not path.exists():
            return {"ok": False, "error": "library file missing"}
        ext = (path.suffix.lstrip(".") or "mkv").lower()
        mime = {
            "mkv": "video/x-matroska", "mp4": "video/mp4", "ts": "video/mpeg",
            "avi": "video/x-msvideo",
        }.get(ext, "application/octet-stream")
        try:
            session_obj = await proxy.start_library_session(path, item["title"] or "", mime)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:160]}
        lan_ip = proxy.lan_ip_for("192.168.1.1")
        urls = urlsign.urls_for(session_obj.token, lan_ip, ext=ext)
        return {
            "ok": True, "token": session_obj.token, "title": item["title"], "mime": mime,
            "mode": "library", "duration": 0.0, **urls,
        }

    async def api_app_stream(media_key: str, start_position: float = 0.0, mode: str = "cache",
                             quality: str = "copy", prepare: int = 0, format: str = "",
                             declare_current: int = 1, tv: str = "",
                             audio: int = -1, sub: int = -1) -> dict:
        payload = await _resolve_media_key(media_key)
        if not payload:
            return {"ok": False, "error": f"media_key {media_key} not found"}
        audio_index = int(audio) if int(audio) >= 0 else None
        sub_index = int(sub) if int(sub) >= 0 else None
        fmt = (format or "").lower()
        if fmt == "mp4":
            actual_mode, mime, served_ext = "cache_mp4", "video/mp4", "mp4"
        elif mode == "virtual":
            actual_mode, mime, served_ext = "virtual", "video/mpeg", "ts"
        elif quality == "downgrade":
            actual_mode, mime, served_ext = "cache_downgrade", "video/mpeg", "ts"
        else:
            actual_mode = "cache"
            mime = "video/x-matroska" if payload["ext"] in ("mkv", "mp4") else "video/mpeg"
            served_ext = "mkv" if mime == "video/x-matroska" else "ts"
        deferred = bool(prepare) and actual_mode.startswith("cache")
        if audio_index is not None and actual_mode == "cache":
            actual_mode = "cache_remux"
            mime = "video/x-matroska"
            served_ext = "mkv"
        try:
            if actual_mode == "virtual":
                s = await proxy.start_virtual_session(
                    payload["url"], payload["title"],
                    transcode=(quality == "downgrade"),
                    start_offset=float(start_position or 0.0),
                    declare_current=True,
                    audio_index=audio_index,
                )
            else:
                s = await proxy.start_session(
                    payload["url"], payload["title"],
                    mode=actual_mode,
                    mime=mime,
                    start_offset=float(start_position or 0.0),
                    declare_current=bool(declare_current),
                    deferred=deferred,
                    audio_index=audio_index,
                    sub_index=sub_index,
                )
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        if s.mime == "video/mp4":
            served_ext = "mp4"
        elif s.mime == "video/x-matroska":
            served_ext = "mkv"
        elif s.mime == "video/mpeg":
            served_ext = "ts"
        if tv:
            s.tv_name = tv
        from . import urlsign

        duration = s.duration_s or 0.0
        if not duration and not deferred:
            try:
                duration = (await probe_duration(payload["url"])) or 0.0
                s.duration_s = duration
            except Exception:
                duration = 0.0
        lan_ip = proxy.lan_ip_for("192.168.1.1")
        urls = urlsign.urls_for(s.token, lan_ip, ext=served_ext)
        return {
            "ok": True, "token": s.token, "title": payload["title"],
            "mime": s.mime, "duration": duration, "mode": s.mode,
            "format": served_ext, "prepared": deferred,
            "live": bool(s.is_live),
            "start_offset": float(start_position or 0.0), **urls,
        }

    async def api_tracks(media_key: str, force: int = 0) -> dict:
        payload = await _resolve_media_key(media_key)
        if not payload:
            return {"ok": False, "error": f"media_key {media_key} not found", "audio": [], "subs": []}
        from . import trackprobe

        data = await trackprobe.probe_and_cache(media_key, payload["url"], force=bool(force))
        return {"ok": True, "media_key": media_key, "title": payload["title"], **data}

    async def api_app_session(token: str) -> dict:
        s = proxy.get_session(token)
        if s is None or s.done:
            return {"ok": False, "error": "session ended"}
        s.last_client_seen = asyncio.get_running_loop().time()
        pos = proxy.progress_seconds(s)
        return {
            "ok": True, "token": token, "mode": s.mode, "mime": s.mime,
            "position": pos[0] if pos else None,
            "duration": pos[1] if pos else s.duration_s,
            "max_served": s.max_served, "clients": s.clients,
        }

    async def api_app_control(token: str, action: str, value: float = 0.0) -> dict:
        s = proxy.get_session(token)
        if s is None:
            return {"ok": False, "error": "unknown session"}
        if action == "stop":
            await proxy.stop_session(token)
            return {"ok": True}
        if action == "seek":
            try:
                ok, detail = await proxy.seek_session(token, float(value))
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200]}
            return {"ok": bool(ok), "detail": detail}
        if action in ("pause", "resume", "play"):
            if not s.tv_name:
                return {"ok": False, "error": "no TV attached to this session"}
            target = await _resolve_renderer(s.tv_name)
            if target is None:
                return {"ok": False, "error": "TV not found or off"}
            try:
                if isinstance(target, CastTarget):
                    from .cc import pause as cc_pause
                    from .cc import resume as cc_resume

                    ok = (cc_resume if action != "pause" else cc_pause)(target.ip)
                else:
                    from .caster import pause as dlna_pause
                    from .caster import resume as dlna_resume

                    ok = await (dlna_resume if action != "pause" else dlna_pause)(target)
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:160]}
            return {"ok": bool(ok), "state": "playing" if action != "pause" else "paused"}
        return {"ok": False, "error": f"unsupported action {action}"}

    async def api_app_sessions() -> dict:
        out = []
        for s in proxy.list_sessions():
            pos = proxy.progress_seconds(s)
            out.append({
                "token": s.token,
                "title": s.title,
                "mode": s.mode,
                "mime": s.mime,
                "position": round(pos[0], 1) if pos else None,
                "duration": round(pos[1], 1) if pos else round(s.duration_s or 0.0, 1),
                "max_served": s.max_served,
                "clients": s.clients,
                "live": bool(s.is_live),
                "tv": s.tv_name,
                "started_at": s.started_at,
            })
        active = None
        if active_play is not None:
            active = {
                "tv": active_play.tv_name,
                "paused": active_play.paused,
                "token": active_play.proxy_token,
            }
        return {"ok": True, "sessions": out, "active_play": active}

    async def api_live_categories() -> list:
        cats = await provider.live_categories()
        streams = await provider.live_streams()
        counts: dict[int, int] = {}
        for s in streams:
            cid = int(s.get("category_id") or 0)
            counts[cid] = counts.get(cid, 0) + 1
        return [
            {"id": int(c.get("category_id") or 0), "name": c.get("category_name") or "?",
             "count": counts.get(int(c.get("category_id") or 0), 0)}
            for c in cats
        ]

    async def api_live_list(q: str = "", category_id: int = 0) -> list:
        streams = await provider.live_streams(category_id or None)
        ql = (q or "").lower().strip()
        usable = [s for s in streams if not (s.get("name") or "").strip().startswith("=")]
        hits = [s for s in usable if ql in (s.get("name") or "").lower()] if ql else usable
        return [
            {"kind": "live", "channel_id": int(s["stream_id"]), "title": s.get("name") or "",
             "category_id": int(s.get("category_id") or 0), "icon": s.get("stream_icon")}
            for s in hits[:200]
        ]

    async def api_live_stream(channel_id: int) -> dict:
        from . import urlsign
        streams = await provider.live_streams()
        ch = next((c for c in streams if int(c.get("stream_id", 0)) == channel_id), None)
        if ch is None:
            return {"ok": False, "error": "channel not found"}
        title = ch.get("name") or f"live {channel_id}"
        try:
            s = await proxy.start_session(provider.live_url(channel_id), title,
                                          mode="copy", mime="video/mpeg", live=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        lan_ip = proxy.lan_ip_for("192.168.1.1")
        urls = urlsign.urls_for(s.token, lan_ip, ext="ts")
        return {"ok": True, "token": s.token, "title": title, "mime": "video/mpeg",
                "mode": "live", "duration": 0.0, "start_offset": 0.0, "live": True, **urls}

    async def api_app_config() -> dict:
        import socket
        from . import urlsign
        from .version import __version__
        base = urlsign.public_base()
        return {
            "ok": True, "version": __version__,
            "lan_ip": proxy.lan_ip_for("192.168.1.1"),
            "public_base": base, "has_public": bool(base),
            "server_name": socket.gethostname(),
        }

    async def api_tv_detail(name: str) -> dict:
        from .capabilities import derive_cast_methods
        conn = store.connect()
        row = store.find_tv_by_name(conn, name)
        if row is None:
            conn.close()
            return {"ok": False, "error": "TV not found"}
        protocols = store.get_tv_sink_protocols(conn, row["udn"])
        stored = store.get_tv_cast_methods(conn, row["udn"])
        conn.close()
        methods = (stored or {}).get("methods") or derive_cast_methods(row["kind"], None, protocols)
        tv = {k: row[k] for k in row.keys() if k != "sink_protocols"}
        return {"ok": True, "tv": tv, "protocols": protocols, "methods": methods,
                "last_probe": (stored or {}).get("at")}

    async def api_tv_probe(name: str) -> dict:
        from datetime import datetime, timezone
        from .capabilities import derive_cast_methods, read_capabilities
        from .scanner import scan_ip
        conn = store.connect()
        row = store.find_tv_by_name(conn, name)
        conn.close()
        if row is None:
            return {"ok": False, "error": "TV not found"}
        ip = row["ip"]
        if not ip:
            return {"ok": False, "error": "TV has no IP on record"}
        scan = await scan_ip(ip)

        async def _open(port: int) -> bool:
            try:
                _r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 1.5)
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
                return True
            except Exception:
                return False

        ports = {}
        for port in (8009, 8060, 8008, 7000, 3000, 3001):
            ports[str(port)] = await _open(port)
        location = scan.get("location") or row["location"]
        protocols = None
        if location:
            try:
                protocols = await read_capabilities(location)
            except Exception:
                protocols = None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        methods = derive_cast_methods(scan.get("kind") or row["kind"], ports, protocols)
        for m in methods:
            m["verified_at"] = now
        conn = store.connect()
        if protocols:
            store.set_tv_sink_protocols(conn, row["udn"], protocols)
        store.set_tv_cast_methods(conn, row["udn"], {"methods": methods, "at": now, "ports": ports})
        conn.close()
        return {"ok": True, "tv": {k: row[k] for k in row.keys() if k != "sink_protocols"},
                "ip": ip, "scan": scan, "ports": ports, "protocols": protocols, "methods": methods}

    async def api_audit_now() -> dict:
        findings = await auditor.run_audit(force=True)
        for f in findings:
            if str(f.get("severity", "")).lower() == "high" and f.get("notify", True):
                await _notify_owner(
                    f"🕵️ Audit found a high-severity issue:\n"
                    f"<b>{f.get('feature','?')}</b>: {f.get('observation','')[:300]}\n"
                    f"Cause: {f.get('likely_cause','?')[:200]}"
                )
                break
        return {"ok": True, "new": len(findings), "findings": findings}

    from .web import bind as web_bind
    web_bind(loop=asyncio.get_running_loop(), search=api_search, episodes=api_episodes,
             tvs=api_tvs, play=api_play, live_play=api_live_play, control=api_control, status=api_status,
             test_virtual=api_test_virtual,
             app_stream=api_app_stream, app_session=api_app_session, app_sessions=api_app_sessions,
             tracks=api_tracks,
             app_control=api_app_control,
             app_config=api_app_config, tv_detail=api_tv_detail, tv_probe=api_tv_probe,
             banned_tvs=api_banned_tvs, ban_tv=api_ban_tv, unban_tv=api_unban_tv,
             audit_now=api_audit_now,
             live_categories=api_live_categories, live_list=api_live_list, live_stream=api_live_stream,
             discover=api_discover, discover_ai=api_discover_ai, similar=api_similar,
             continue_watching=api_continue, progress=api_progress, set_progress=api_set_progress,
             health=api_health, events=api_events, client_log=api_client_log,
             tokens_create=api_tokens_create, tokens_list=api_tokens_list,
             tokens_revoke=api_tokens_revoke, library_stream=api_library_stream,
             pair_notify=api_pair_notify)

    @dp.message(Command("audit"))
    async def cmd_audit(message: Message) -> None:
        wait = await message.answer("🔍 Running self-audit…")
        findings = await auditor.run_audit(force=True)
        if not findings:
            await wait.edit_text("✅ Audit clean — no new anomalies found.")
            return
        lines = []
        for f in findings[:8]:
            lines.append(f"[{str(f.get('severity','?')).upper()}] {f.get('feature','?')}: {f.get('observation','')[:180]}")
        await wait.edit_text("🕵️ Findings:\n" + "\n".join(lines) + "\n\nFull notes: data/audit/NOTES.md")
    logger.info("Bot @%s starting polling…", (await bot.get_me()).username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
