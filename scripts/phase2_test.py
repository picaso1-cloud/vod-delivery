import argparse
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.capabilities import (
    best_delivery_mime,
    choose_protocol_info,
    read_capabilities,
)
from src.caster import MIME_BY_EXT

API = "http://127.0.0.1:8095"
DEFAULT_FILE = "/tmp/opencode/soak-source.mp4"


def find_tv(name: str):
    conn = store.connect()
    row = conn.execute(
        "SELECT * FROM tvs WHERE name LIKE ? AND ignored=0", (f"%{name}%",)).fetchone()
    return conn, row


def probe_tv(conn, row: dict) -> dict | None:
    location = row["location"]
    if not location:
        print("no DLNA location (cast TV) — skipping GetProtocolInfo")
        return None
    print(f"probing {row['name']} @ {location} ...")
    res = asyncio.run(read_capabilities(location))
    if not res.get("ok"):
        print(f"probe FAILED: {res.get('error') or res.get('protocol_error')}")
        return None
    payload = {
        "ok": True,
        "mimes": res.get("mimes", []),
        "profiles": res.get("profiles", []),
        "ops": res.get("ops", []),
        "flags": res.get("flags", []),
        "count": res.get("count", 0),
        "transport_state": res.get("transport_state"),
    }
    store.set_tv_sink_protocols(conn, row["udn"], payload)
    print(f"declared: {res.get('count')} sink entries, transport={res.get('transport_state')}")
    print(f"  video MIMEs: {', '.join(m for m in res.get('mimes', []) if m.startswith('video/'))}")
    for ext in ("ts", "mkv", "mp4"):
        mime = best_delivery_mime(ext, payload, MIME_BY_EXT.get(ext, "video/mpeg"))
        print(f"  {ext:4s} -> {mime:26s} protocolInfo={choose_protocol_info(mime, payload)}")
    return payload


def api_status() -> dict:
    with urllib.request.urlopen(f"{API}/api/status", timeout=5) as resp:
        return json.loads(resp.read())


def cast_loop(tv: str, file_path: str, transcode: bool) -> dict:
    query = urllib.parse.urlencode({
        "tv": tv,
        "file_path": file_path,
        "transcode": "true" if transcode else "false",
        "loop": "true",
    })
    req = urllib.request.Request(f"{API}/api/test/virtual?{query}", method="POST")
    with urllib.request.urlopen(req, timeout=220) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv", required=True)
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--no-transcode", action="store_true")
    args = ap.parse_args()

    if not Path(args.file).exists():
        print(f"source file missing: {args.file}")
        return 2

    conn, row = find_tv(args.tv)
    if row is None:
        print(f"TV not found: {args.tv}")
        conn.close()
        return 2
    try:
        probe_tv(conn, row)
    finally:
        conn.close()

    print(f"\ncasting looping source to {row['name']} ...")
    try:
        result = cast_loop(row["name"], args.file, not args.no_transcode)
    except Exception as exc:
        print(f"cast call failed: {type(exc).__name__} {exc}")
        return 1
    print("cast result:", json.dumps(result))
    if not result.get("ok"):
        print("VERDICT: cast rejected / no TV consumption")
        return 1

    started = time.time()
    end = started + args.minutes * 60
    last = None
    while time.time() < end:
        try:
            st = api_status()
        except Exception as exc:
            print(f"status error: {exc}")
            time.sleep(10)
            continue
        pos = st.get("position") or {}
        current = (st.get("playing"), pos.get("position"), pos.get("duration"))
        if current != last:
            elapsed = int(time.time() - started)
            print(f"[{elapsed:4d}s] playing={st.get('playing')} "
                  f"pos={pos.get('position')} dur={pos.get('duration')}")
            last = current
        if not st.get("playing"):
            print("VERDICT: playback stopped before the window ended")
            return 1
        time.sleep(10)
    print(f"VERDICT: still playing after {args.minutes:.0f} minutes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
