import logging
import threading
import uuid

logger = logging.getLogger("vod-delivery")

_lock = threading.Lock()
_casts: dict[str, object] = {}


def _get(host: str):
    import pychromecast
    from pychromecast.discovery import CastInfo, HostServiceInfo

    with _lock:
        if host not in _casts:
            info = CastInfo(
                services={HostServiceInfo(host, 8009)},
                uuid=uuid.uuid3(uuid.NAMESPACE_DNS, host),
                model_name="Cast TV",
                friendly_name=host,
                host=host,
                port=8009,
                cast_type="cast",
                manufacturer="",
            )
            cc = pychromecast.get_chromecast_from_cast_info(info, zconf=None)
            cc.wait(timeout=20)
            _casts[host] = cc
        return _casts[host]


def play(host: str, url: str, mime: str = "video/mp4", title: str = "", subs_url: str = "") -> str:
    logger.info("chromecast play %s → %s (%s) subs=%s", host, url, mime, bool(subs_url))
    cc = _get(host)
    mc = cc.media_controller
    kwargs = {}
    if subs_url:
        kwargs = {"subtitles": subs_url, "subtitles_mime": "text/vtt", "subtitles_lang": "ara"}
    mc.play_media(url, mime, title=title, **kwargs)
    mc.block_until_active(timeout=30)
    logger.info("chromecast app active on %s: %s", host, cc_status(host))
    return "PLAYING"


def stop(host: str) -> None:
    try:
        cc = _casts.get(host)
        if cc:
            cc.quit_app()
    except Exception:
        logger.exception("cast stop failed")


def volume(host: str, delta: float = 0.1, mute: bool | None = None) -> tuple[float, bool] | None:
    try:
        cc = _get(host)
        cc.wait(timeout=10)
        if mute is None:
            level = float(getattr(cc.status, "volume_level", 0.5) or 0.5)
            level = max(0.0, min(1.0, level + delta))
            cc.set_volume(level)
        else:
            cc.set_muted(mute)
            level = float(getattr(cc.status, "volume_level", 0.5) or 0.5)
        return (level, bool(getattr(cc.status, "volume_muted", False)))
    except Exception:
        logger.exception("cast volume failed")
        return None


def status(host: str) -> str:
    try:
        cc = _casts.get(host)
        if cc is None:
            return "IDLE"
        cc.media_controller.update_status()
        return cc.media_controller.status.player_state
    except Exception:
        return "IDLE"


def media_status(host: str) -> tuple[str, float, float] | None:
    try:
        cc = _casts.get(host)
        if cc is None:
            return None
        cc.media_controller.update_status()
        ms = cc.media_controller.status
        return ms.player_state, float(ms.current_time or 0), float(ms.duration or 0)
    except Exception:
        return None


def seek(host: str, position: float) -> bool:
    try:
        cc = _casts.get(host)
        if cc is None:
            return False
        cc.media_controller.update_status()
        cc.media_controller.seek(position)
        return True
    except Exception:
        logger.exception("cc seek failed")
        return False


def pause(host: str) -> bool:
    try:
        cc = _casts.get(host)
        if cc is None:
            return False
        cc.media_controller.pause()
        return True
    except Exception:
        return False


def resume(host: str) -> bool:
    try:
        cc = _casts.get(host)
        if cc is None:
            return False
        cc.media_controller.play()
        return True
    except Exception:
        return False
