import asyncio
import re

AVTRANSPORT_URN = "urn:schemas-upnp-org:service:AVTransport:1"
CONNECTION_MANAGER_URN = "urn:schemas-upnp-org:service:ConnectionManager:1"

MINIMAL_OP = "01"


def parse_sink_protocols(xml: str) -> list:
    m = re.search(r"<SinkProtocolInfo>(.*?)</SinkProtocolInfo>", xml, re.S)
    if not m:
        m = re.search(r"<Sink>(.*?)</Sink>", xml, re.S)
    if not m:
        return []
    return [entry.strip() for entry in m.group(1).split(",") if entry.strip()]


def digest_protocols(entries: list) -> dict:
    mimes = []
    profiles = []
    ops = []
    flags = []
    for entry in entries:
        parts = entry.split(":")
        if len(parts) >= 4 and parts[2] and parts[2] not in mimes:
            mimes.append(parts[2])
        for prof in re.findall(r"DLNA\.ORG_PN=([A-Za-z0-9_]+)", entry):
            if prof not in profiles:
                profiles.append(prof)
        for op in re.findall(r"DLNA\.ORG_OP=([0-9A-Fa-f]+)", entry):
            if op not in ops:
                ops.append(op)
        for flag in re.findall(r"DLNA\.ORG_FLAGS=([0-9A-Fa-f]+)", entry):
            if flag not in flags:
                flags.append(flag)
    return {
        "mimes": sorted(mimes),
        "profiles": sorted(profiles),
        "ops": sorted(ops),
        "flags": sorted(flags),
        "count": len(entries),
        "entries": entries,
    }


def supports_mime(sink_mimes: list, mime: str) -> bool:
    norm = (mime or "").lower()
    base = norm.split(";")[0].strip()
    for candidate in sink_mimes:
        cand = candidate.lower().split(";")[0].strip()
        if cand == base or cand == "*" or cand == "*/*":
            return True
        if cand.endswith("/*") and base.startswith(cand[:-1]):
            return True
    return False


def minimal_protocol_info(mime: str) -> str:
    return f"http-get:*:{mime}:DLNA.ORG_OP={MINIMAL_OP}"


def choose_protocol_info(mime: str, digest: dict | None = None) -> str:
    return minimal_protocol_info(mime)


DELIVERY_CANDIDATES = {
    "ts": ["video/mpeg", "video/vnd.dlna.mpeg-tts"],
    "mkv": ["video/x-mkv", "video/x-matroska"],
    "mp4": ["video/mp4", "video/mpeg"],
    "avi": ["video/avi", "video/mpeg"],
    "mkv-h264": ["video/x-matroska", "video/mp4"],
}


def best_delivery_mime(ext: str, digest: dict | None, fallback_mime: str) -> str:
    if not digest:
        return fallback_mime
    sink = digest.get("mimes") or []
    for candidate in DELIVERY_CANDIDATES.get(ext, []):
        if supports_mime(sink, candidate):
            return candidate
    return fallback_mime


def delivery_supported(ext: str, digest: dict | None) -> bool:
    if not digest:
        return True
    sink = digest.get("mimes") or []
    return any(supports_mime(sink, candidate) for candidate in DELIVERY_CANDIDATES.get(ext, []))


def find_control_url(description: str, urn: str) -> str | None:
    for block in re.findall(r"<service>(.*?)</service>", description, re.S):
        if urn in block:
            ctl = re.search(r"<controlURL>(.*?)</controlURL>", block, re.S)
            if ctl:
                return ctl.group(1).strip()
    return None


def origin_of(location: str) -> str:
    m = re.match(r"(https?://[^/]+)", location or "")
    return m.group(1) if m else (location or "")


async def fetch_description(location: str, timeout: float = 8.0) -> str:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(location, timeout=timeout) as resp:
            return await resp.text()


async def soap_call(location: str, control_url: str, urn: str, action: str,
                    inner: str = "", timeout: float = 12.0) -> tuple:
    import aiohttp

    url = control_url if control_url.startswith("http") else origin_of(location).rstrip("/") + "/" + control_url.lstrip("/")
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{urn}">{inner}</u:{action}></s:Body></s:Envelope>'
    )
    headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{urn}#{action}"'}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
            return resp.status, await resp.text()


async def read_capabilities(location: str) -> dict:
    result = {"location": location, "ok": False}
    try:
        description = await fetch_description(location)
    except Exception as exc:
        result["error"] = f"description: {type(exc).__name__} {exc}"
        return result
    control = find_control_url(description, CONNECTION_MANAGER_URN)
    av_control = find_control_url(description, AVTRANSPORT_URN)
    result["has_connection_manager"] = bool(control)
    result["has_avtransport"] = bool(av_control)
    if control:
        try:
            status, body = await soap_call(location, control, CONNECTION_MANAGER_URN, "GetProtocolInfo")
            entries = parse_sink_protocols(body)
            digest = digest_protocols(entries)
            result.update(digest)
            result["status"] = status
            result["sink_protocols"] = entries
            result["ok"] = bool(entries)
        except Exception as exc:
            result["protocol_error"] = f"{type(exc).__name__} {exc}"
    if av_control:
        try:
            status, body = await soap_call(location, av_control, AVTRANSPORT_URN, "GetTransportInfo",
                                           "<InstanceID>0</InstanceID>", timeout=8.0)
            state = re.search(r"<CurrentTransportState>([^<]+)</CurrentTransportState>", body)
            result["transport_state"] = state.group(1).strip() if state else f"HTTP {status}"
        except Exception as exc:
            result["transport_state"] = f"{type(exc).__name__} {exc}"
    return result


CAST_METHOD_PORTS = {
    "cast": 8009,
    "dial": 8008,
    "roku": 8060,
    "airplay": 7000,
    "webos": 3001,
}


def derive_cast_methods(kind: str | None, ports: dict | None = None,
                        protocols: dict | None = None) -> list:
    ports = ports or {}
    entries = (protocols or {}).get("mimes") or (protocols or {}).get("entries") or []
    methods = [{
        "method": "dlna",
        "available": kind == "dlna",
        "detail": f"UPnP AVTransport, {len(entries)} protocols" if entries else ("UPnP AVTransport" if kind == "dlna" else "not detected"),
    }]
    for name, port in CAST_METHOD_PORTS.items():
        if name == "cast":
            open_ = bool(ports.get("8009")) or kind == "cast"
        else:
            open_ = bool(ports.get(str(port)))
        methods.append({
            "method": name,
            "available": open_,
            "detail": f"port {port} open" if open_ else f"port {port} closed",
        })
    return methods
