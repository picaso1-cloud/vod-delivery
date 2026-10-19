import asyncio
import logging
from xml.sax.saxutils import escape

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.const import HttpRequest
from async_upnp_client.exceptions import UpnpError

from .ssdp import Renderer

logger = logging.getLogger("vod-delivery")

MIME_BY_EXT = {
    "mkv": "video/x-matroska",
    "mp4": "video/mp4",
    "ts": "video/mpeg",
    "m3u8": "application/vnd.apple.mpegurl",
    "avi": "video/x-msvideo",
}

DLNA_PROFILE_BY_EXT = {
    "mkv": "MATROSKA",
    "mp4": "AVC_MP4_MP_SD_AAC_MULT5",
    "ts": "MPEG_TS_SD_NA_ISO",
    "avi": "AVI",
}

DLNA_FLAGS = "DLNA.ORG_OP=11;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000"

_DIDL_NS = (
    'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"'
)


class MpostFallbackRequester(AiohttpRequester):
    async def async_http_request(self, http_request: HttpRequest):
        response = await super().async_http_request(http_request)
        if response.status_code != 405 or http_request.method != "POST":
            return response
        headers = {k: v for k, v in (http_request.headers or {}).items()}
        soap_action = headers.pop("SOAPACTION", headers.pop("SoapAction", ""))
        mpost_headers = {
            **headers,
            'MAN': '"http://schemas.xmlsoap.org/soap/envelope/"; ns=01',
            "01-SOAPACTION": soap_action,
        }
        mpost = HttpRequest(method="M-POST", url=http_request.url, headers=mpost_headers, body=http_request.body)
        logger.debug("SOAP POST got 405 — retrying as M-POST")
        return await super().async_http_request(mpost)


def content_features(extension: str) -> str:
    profile = DLNA_PROFILE_BY_EXT.get(extension)
    prefix = f"DLNA.ORG_PN={profile};" if profile else ""
    return f"{prefix}{DLNA_FLAGS}"


def _didl(title: str, uri: str, mime: str, duration_s: float | None = None,
          protocol_info: str | None = None) -> str:
    duration_attr = ""
    if duration_s and duration_s > 0:
        duration_attr = f' duration="{int(duration_s // 3600):01d}:{int(duration_s % 3600 // 60):02d}:{int(duration_s % 60):02d}"'
    info = protocol_info or f"http-get:*:{mime}:{content_features(mime_to_ext(mime))}"
    return (
        f"<DIDL-Lite {_DIDL_NS}>"
        '<item id="0" parentID="0" restricted="0">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<upnp:class>object.item.videoItem.movie</upnp:class>"
        f'<res protocolInfo="{info}"{duration_attr}>{escape(uri)}</res>'
        "</item></DIDL-Lite>"
    )


def mime_to_ext(mime: str) -> str:
    for ext, candidate in MIME_BY_EXT.items():
        if candidate == mime:
            return ext
    return "mkv"


def _unescape_didl(didl: str) -> str:
    return didl.replace("&#34;", '"').replace("&#39;", "'").replace("&amp;", "&")


def _is_wedge_error(exc: Exception) -> bool:
    code = getattr(exc, "error_code", None)
    text = str(exc)
    return code == 701 or "701" in text or "transition not available" in text.lower()


async def probe_duration(path) -> float | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        return float(stdout.decode().strip())
    except Exception:
        return None


async def _rendering_control(device):
    return next(s for s in device.services.values() if "RenderingControl" in s.service_type)


async def volume_step(renderer: Renderer, delta: int) -> int | None:
    try:
        factory = UpnpFactory(MpostFallbackRequester())
        device = await factory.async_create_device(renderer.location)
        rc = await _rendering_control(device)
        try:
            current = await rc.action("GetVolume").async_call(InstanceID=0, Channel="Master")
            level = int(current.get("CurrentVolume") or 0)
        except Exception:
            level = 30
        level = max(0, min(100, level + delta))
        await rc.action("SetVolume").async_call(InstanceID=0, Channel="Master", DesiredVolume=level)
        return level
    except Exception:
        logger.exception("dlna volume_step failed")
        return None


async def set_mute(renderer: Renderer, muted: bool) -> bool:
    try:
        factory = UpnpFactory(MpostFallbackRequester())
        device = await factory.async_create_device(renderer.location)
        rc = await _rendering_control(device)
        await rc.action("SetMute").async_call(InstanceID=0, Channel="Master", DesiredMute="1" if muted else "0")
        return True
    except Exception:
        logger.exception("dlna set_mute failed")
        return False


def _avtransport(device):
    return next(s for s in device.services.values() if "AVTransport" in s.service_type)


async def _transport_state(device) -> str:
    try:
        state = await _avtransport(device).action("GetTransportInfo").async_call(InstanceID=0)
        return state.get("CurrentTransportState", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


async def cast(renderer: Renderer, uri: str, title: str, extension: str = "mkv",
               duration_s: float | None = None, protocol_info: str | None = None) -> str:
    mime = MIME_BY_EXT.get(extension, "video/x-matroska")
    factory = UpnpFactory(MpostFallbackRequester(timeout=45))
    device = await factory.async_create_device(renderer.location)
    service = _avtransport(device)

    didl = _didl(title, uri, mime, duration_s, protocol_info)
    try:
        await service.action("SetAVTransportURI").async_call(InstanceID=0, CurrentURI=uri, CurrentURIMetaData=didl)
    except UpnpError as exc:
        if _is_wedge_error(exc):
            logger.warning("701/transition-not-available on SetAVTransportURI — Stop then retry")
            try:
                await service.action("Stop").async_call(InstanceID=0)
                await asyncio.sleep(0.8)
                await service.action("SetAVTransportURI").async_call(InstanceID=0, CurrentURI=uri, CurrentURIMetaData=didl)
            except UpnpError as exc2:
                if _is_wedge_error(exc2):
                    raise
                logger.info("DIDL unescape compat retry after %s", exc2)
                legacy = _didl(title, uri, mime, duration_s)
                await service.action("SetAVTransportURI").async_call(
                    InstanceID=0, CurrentURI=uri, CurrentURIMetaData=_unescape_didl(legacy))
        else:
            logger.info("SetAVTransportURI failed (%s) — retrying with unescaped DIDL", exc)
            await service.action("SetAVTransportURI").async_call(
                InstanceID=0, CurrentURI=uri, CurrentURIMetaData=_unescape_didl(didl))

    await asyncio.sleep(0.7)
    await service.action("Play").async_call(InstanceID=0, Speed="1")
    await asyncio.sleep(1.0)
    return await _transport_state(device)


async def stop(renderer: Renderer) -> None:
    factory = UpnpFactory(AiohttpRequester())
    device = await factory.async_create_device(renderer.location)
    await _avtransport(device).action("Stop").async_call(InstanceID=0)


async def playback_state(renderer: Renderer) -> str:
    factory = UpnpFactory(AiohttpRequester())
    device = await factory.async_create_device(renderer.location)
    return await _transport_state(device)


def _parse_dlna_time(raw: str | None) -> float:
    if not raw:
        return 0.0
    parts = raw.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


async def get_position(renderer: Renderer) -> tuple[float, float]:
    factory = UpnpFactory(AiohttpRequester())
    device = await factory.async_create_device(renderer.location)
    info = await _avtransport(device).action("GetPositionInfo").async_call(InstanceID=0)
    return _parse_dlna_time(info.get("RelTime")), _parse_dlna_time(info.get("TrackDuration"))


async def seek(renderer: Renderer, seconds: float) -> bool:
    factory = UpnpFactory(AiohttpRequester())
    device = await factory.async_create_device(renderer.location)
    target = f"{int(seconds // 3600):01d}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"
    try:
        await _avtransport(device).action("Seek").async_call(InstanceID=0, Unit="REL_TIME", Target=target)
        return True
    except Exception:
        return False


async def pause(renderer: Renderer) -> bool:
    try:
        factory = UpnpFactory(AiohttpRequester())
        device = await factory.async_create_device(renderer.location)
        await _avtransport(device).action("Pause").async_call(InstanceID=0)
        return True
    except Exception:
        return False


async def resume(renderer: Renderer) -> bool:
    try:
        factory = UpnpFactory(AiohttpRequester())
        device = await factory.async_create_device(renderer.location)
        await _avtransport(device).action("Play").async_call(InstanceID=0, Speed="1")
        return True
    except Exception:
        return False
