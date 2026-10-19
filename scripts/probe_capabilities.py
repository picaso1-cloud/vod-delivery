import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "vod.db"
OUT = ROOT / "data" / "capabilities"

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"


def services(desc):
    found = {}
    for block in re.findall(r"<service>(.*?)</service>", desc, re.S):
        urn = re.search(r"<serviceType>(.*?)</serviceType>", block, re.S)
        ctl = re.search(r"<controlURL>(.*?)</controlURL>", block, re.S)
        if urn and ctl:
            found[urn.group(1).strip()] = ctl.group(1).strip()
    return found


def origin(loc):
    m = re.match(r"(https?://[^/]+)", loc)
    return m.group(1) if m else loc


async def soap(session, origin_url, control, urn, action, inner="", timeout=15):
    url = control if control.startswith("http") else origin_url.rstrip("/") + "/" + control.lstrip("/")
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{urn}">{inner}</u:{action}></s:Body></s:Envelope>'
    )
    headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{urn}#{action}"'}
    async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
        return resp.status, await resp.text()


async def port_open(host, port, timeout=2.0):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        return True
    except Exception:
        return False


def parse_protocols(xml):
    from src.capabilities import parse_sink_protocols
    return parse_sink_protocols(xml)


async def probe_dlna(session, name, ip, location):
    result = {"name": name, "ip": ip, "location": location, "ok": False}
    try:
        async with session.get(location, timeout=8) as resp:
            desc = await resp.text()
        result["description_status"] = resp.status
    except Exception as exc:
        result["error"] = f"description: {type(exc).__name__} {exc}"
        return result
    svcs = services(desc)
    result["services"] = sorted(svcs.keys())
    origin_url = origin(location)
    cm_ctl = svcs.get(CM)
    if cm_ctl:
        try:
            status, body = await soap(session, origin_url, cm_ctl, CM, "GetProtocolInfo", timeout=12)
            entries = parse_protocols(body)
            mimes = sorted({e.split(":")[2] for e in entries if e.count(":") >= 3})
            profiles = sorted({p for e in entries for p in re.findall(r"DLNA\.ORG_PN=([A-Za-z0-9_]+)", e)})
            ops = sorted({p for e in entries for p in re.findall(r"DLNA\.ORG_OP=([0-9A-Fa-f]+)", e)})
            flags = sorted({p for e in entries for p in re.findall(r"DLNA\.ORG_FLAGS=([0-9A-Fa-f]+)", e)})
            result.update({
                "protocol_status": status,
                "sink_count": len(entries),
                "mimes": mimes,
                "dlna_profiles": profiles,
                "dlna_op": ops,
                "dlna_flags": flags,
                "sink_protocols": entries,
                "ok": True,
            })
        except Exception as exc:
            result["cm_error"] = f"{type(exc).__name__} {exc}"
    avt_ctl = svcs.get(AVT)
    if avt_ctl:
        try:
            status, body = await soap(session, origin_url, avt_ctl, AVT, "GetTransportInfo",
                                      "<InstanceID>0</InstanceID>", timeout=10)
            m = re.search(r"<CurrentTransportState>([^<]+)</CurrentTransportState>", body)
            result["avtransport"] = m.group(1).strip() if m else f"HTTP {status}"
        except Exception as exc:
            result["avtransport"] = f"{type(exc).__name__} {exc}"
    return result


async def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select name, ip, location, kind, udn from tvs where ignored=0 order by name").fetchall()
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = sys.argv[1].lower() if len(sys.argv) > 1 else None
    results = []
    async with aiohttp.ClientSession() as session:
        for row in rows:
            name, ip, location, kind, udn = row["name"], row["ip"], row["location"], row["kind"], row["udn"]
            if wanted and wanted not in (name or "").lower():
                continue
            print(f"\n=== {name} ({ip}) kind={kind}")
            if not ip or not location:
                print("  no DLNA location (cast/non-DLNA device) — skipping GetProtocolInfo")
                continue
            res = await probe_dlna(session, name, ip, location)
            if res.get("ok"):
                print(f"  GetProtocolInfo: HTTP {res['protocol_status']}, {res['sink_count']} sink entries")
                print(f"  MIMEs: {', '.join(res['mimes'][:20])}")
                print(f"  DLNA profiles: {', '.join(res['dlna_profiles'][:15]) or '(none declared)'}")
                print(f"  OP: {', '.join(res['dlna_op']) or '-'}   FLAGS: {', '.join(res['dlna_flags'][:6]) or '-'}")
                print(f"  AVTransport: {res.get('avtransport', 'n/a')}")
            else:
                print(f"  FAILED: {res.get('error') or res.get('cm_error')}")
            results.append(res)
            if res.get("ok"):
                from src import store
                payload = {
                    "ok": True,
                    "mimes": res.get("mimes", []),
                    "profiles": res.get("dlna_profiles", []),
                    "ops": res.get("dlna_op", []),
                    "flags": res.get("dlna_flags", []),
                    "count": res.get("sink_count", 0),
                    "transport_state": res.get("avtransport"),
                }
                try:
                    store.set_tv_sink_protocols(conn, udn, payload)
                    print("  stored sink_protocols in tvs row")
                except Exception as exc:
                    print(f"  store failed: {exc}")
            if "LIFEMAXX" in (name or "") and ip:
                ssap = {p: await port_open(ip, p) for p in (3000, 3001)}
                other = {p: await port_open(ip, p) for p in (8009, 8008, 8060, 5555)}
                print(f"  SSAP ports: {ssap}")
                print(f"  Cast/DIAL/ADB ports: {other}")
                res["ssap_ports"] = ssap
            if "webOS" in (name or "") and ip:
                air = {p: await port_open(ip, p) for p in (7000, 377)}
                ssap = {p: await port_open(ip, p) for p in (3000, 3001)}
                print(f"  AirPlay ports: {air}  SSAP ports: {ssap}")
                res["airplay_ports"] = air
                res["ssap_ports"] = ssap
    conn.close()
    stamp = OUT / "latest.json"
    stamp.write_text(json.dumps(results, indent=2))
    print(f"\nSaved -> {stamp.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
