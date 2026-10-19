import asyncio
import fcntl
import socket
import struct
from dataclasses import dataclass

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory

SSDP_HOST = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TARGET = "urn:schemas-upnp-org:device:MediaRenderer:1"
_SIOCGIFADDR = 0x8915
_SKIP_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "tailscale", "zt", "tun", "wg")

_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_HOST}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 3\r\n"
    f"ST: {SSDP_TARGET}\r\n"
    "\r\n"
).encode()


@dataclass
class Renderer:
    location: str
    friendly_name: str
    udn: str


def local_ipv4s() -> list[str]:
    ips = []
    for _idx, name in socket.if_nameindex():
        if name.startswith(_SKIP_IFACE_PREFIXES):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                packed = fcntl.ioctl(s.fileno(), _SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
                ips.append(socket.inet_ntoa(packed[20:24]))
            except OSError:
                continue
    return ips


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, sink: asyncio.Queue) -> None:
        self.sink = sink

    def datagram_received(self, data: bytes, _addr) -> None:
        self.sink.put_nowait(data)


async def _ssdp_search(timeout: float) -> dict[str, str]:
    loop = asyncio.get_running_loop()
    sink: asyncio.Queue[bytes] = asyncio.Queue()
    found: dict[str, str] = {}
    transports = []
    for ip in local_ipv4s():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            transport, _ = await loop.create_datagram_endpoint(lambda: _Collector(sink), sock=sock)
            transports.append(transport)
            transport.sendto(_MSEARCH, (SSDP_HOST, SSDP_PORT))
            transport.sendto(_MSEARCH, (SSDP_HOST, SSDP_PORT))
        except OSError:
            continue
    try:
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(sink.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            headers = {}
            for line in data.decode("utf-8", "replace").split("\r\n"):
                if ":" in line:
                    key, _, value = line.partition(":")
                    headers[key.strip().upper()] = value.strip()
            location = headers.get("LOCATION")
            udn = headers.get("USN", "").split("::")[0].strip()
            if location and udn:
                found.setdefault(udn, location)
    finally:
        for transport in transports:
            transport.close()
    return found


async def discover_renderers(timeout: float = 6.0) -> list[Renderer]:
    locations = await _ssdp_search(timeout)
    factory = UpnpFactory(AiohttpRequester())
    renderers: list[Renderer] = []
    for udn, location in locations.items():
        try:
            device = await factory.async_create_device(location)
        except Exception:
            continue
        if any("AVTransport" in s.service_type for s in device.services.values()):
            renderers.append(
                Renderer(
                    location=location,
                    friendly_name=device.name or udn,
                    udn=udn,
                )
            )
    renderers.sort(key=lambda r: r.friendly_name)
    return renderers
