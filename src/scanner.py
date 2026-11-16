import asyncio
import ipaddress

import httpx

from . import store

CAST_PORTS = [8009, 8008]
DLNA_PORTS = [9197, 3500, 49152, 49153, 49154, 8200, 5000, 1310, 7676, 2869, 40000]
DLNA_PATHS = ["", "/dmr", "/description.xml", "/dd.xml", "/DeviceDescription.xml", "/gdev.xml"]


def valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip.strip())
        return True
    except ValueError:
        return False


async def _port_open(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        w.close()
        return True
    except Exception:
        return False


async def scan_ip(ip: str) -> dict:
    ip = ip.strip()
    result = {
        "ip": ip,
        "mac": store.arp_mac(ip),
        "kind": None,
        "name": None,
        "location": None,
        "validated": False,
        "notes": [],
    }
    if not valid_ip(ip):
        result["notes"].append("invalid IP")
        return result

    if await _port_open(ip, CAST_PORTS[0]):
        result["notes"].append(f"cast port {CAST_PORTS[0]} open")
        name = await _identify_cast(ip)
        if name:
            result.update(kind="cast", name=name, validated=True)
            result["notes"].append("Google Cast handshake OK")
            return result
        result.update(kind="cast")
        result["notes"].append("handshake inconclusive — registering as cast anyway (port 8009 is definitive on TVs)")
        return result

    for port in DLNA_PORTS:
        if not await _port_open(ip, port):
            continue
        result["notes"].append(f"port {port} open")
        for path in DLNA_PATHS:
            location = f"http://{ip}:{port}{path}"
            try:
                async with httpx.AsyncClient(timeout=4) as client:
                    response = await client.get(location, follow_redirects=True)
                if "<device" not in response.text or "<friendlyName>" not in response.text:
                    continue
            except httpx.HTTPError:
                continue
            result["notes"].append(f"description found at {location}")
            name, has_avtransport = await _validate_dlna(location)
            if name and has_avtransport:
                result.update(kind="dlna", name=name, location=location, validated=True)
                result["notes"].append("AVTransport service OK — DLNA casting supported")
                return result
            if name:
                result["notes"].append("device found but no AVTransport (no DLNA push)")

    if result["kind"] is None:
        result["notes"].append("no recognizable streaming service on this IP")
    return result


async def _identify_cast(ip: str) -> str | None:
    def _connect() -> str | None:
        try:
            import pychromecast
            from pychromecast.discovery import CastInfo, HostServiceInfo

            info = CastInfo(
                services={HostServiceInfo(ip, 8009)},
                uuid=uuid.uuid3(uuid.NAMESPACE_DNS, ip),
                model_name=None,
                friendly_name=None,
                host=ip,
                port=8009,
                cast_type="cast",
                manufacturer=None,
            )
            cc = pychromecast.get_chromecast_from_cast_info(info, zconf=None)
            cc.wait(timeout=20)
            return cc.cast_info.friendly_name
        except Exception:
            return None

    import asyncio as _asyncio

    first = await _asyncio.to_thread(_connect)
    if first:
        return first
    await _asyncio.sleep(3)
    return await _asyncio.to_thread(_connect)


async def _validate_dlna(location: str) -> tuple[str | None, bool]:
    try:
        from async_upnp_client.aiohttp import AiohttpRequester
        from async_upnp_client.client_factory import UpnpFactory

        factory = UpnpFactory(AiohttpRequester(timeout=10))
        device = await factory.async_create_device(location)
        has_avtransport = any("AVTransport" in s.service_type for s in device.services.values())
        return device.name, has_avtransport
    except Exception:
        return None, False


def register(result: dict, name: str | None = None) -> str | None:
    conn = store.connect()
    try:
        mac = result.get("mac")
        kind = result.get("kind") or "dlna"
        if mac and store.is_banned_mac(conn, mac):
            conn.execute(
                "UPDATE tvs SET ignored=1 WHERE mac=? OR udn=?",
                (store.normalize_mac(mac), f"{kind}::{mac.replace(':', '')}"),
            )
            conn.commit()
            return None
        udn = f"{kind}::{(mac or result['ip']).replace(':', '')}"
        display = name or result.get("name") or f"{kind.upper()} {result['ip']}"
        existing = store.find_tv_by_mac(conn, mac) if mac else None
        if existing:
            keep_name = existing["name"]
            if existing["source"] in ("renamed", "manual") or _looks_auto(keep_name) is False:
                display = keep_name
            conn.execute(
                "UPDATE tvs SET name=?, ip=?, location=COALESCE(?, location), kind=? WHERE udn=?",
                (display, result["ip"], result.get("location"), kind, existing["udn"]),
            )
            conn.commit()
            conn.execute(
                "DELETE FROM tvs WHERE mac=? AND udn != ? AND source='manual' AND ignored=0",
                (mac, existing["udn"]),
            )
            conn.commit()
            return existing["udn"]
        conn.execute(
            "INSERT INTO tvs(udn, name, ip, mac, location, kind, source, last_seen) VALUES(?,?,?,?,?,?, 'manual', datetime('now')) "
            "ON CONFLICT(udn) DO UPDATE SET ip=excluded.ip, location=excluded.location, kind=excluded.kind",
            (udn, display, result["ip"], mac, result.get("location"), kind),
        )
        conn.commit()
        return udn
    finally:
        conn.close()


def _looks_auto(name: str) -> bool:
    return name.startswith(("CAST ", "DLNA ", "TV ", "manual"))
