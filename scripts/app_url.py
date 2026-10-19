import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import urlsign

API = "http://127.0.0.1:8095"


def call(method: str, path: str, **params):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API}{path}?{qs}", method=method)
    req.add_header("Authorization", f"Bearer {urlsign.app_token()}")
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search")
    ap.add_argument("--key")
    ap.add_argument("--mode", default="cache", choices=["cache", "virtual"])
    ap.add_argument("--quality", default="copy", choices=["copy", "downgrade"])
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--stop")
    args = ap.parse_args()

    if args.search:
        for group in call("GET", "/api/app/search", q=args.search, type="movie"):
            for variant in group["variants"]:
                print(f'{group["title"]} ({group["year"]}) [{variant["lang"]}] {variant["media_key"]}')
        return 0

    if args.stop:
        print(call("POST", "/api/app/control", token=args.stop, action="stop"))
        return 0

    if not args.key:
        ap.error("one of --search, --key, --stop is required")

    res = call("POST", "/api/app/stream", media_key=args.key, start_position=args.start,
               mode=args.mode, quality=args.quality)
    print(json.dumps(res, indent=2))
    if res.get("public_url"):
        print("\nPLAY (public):", res["public_url"])
    print("PLAY (LAN)   :", res.get("lan_url"))
    print("STOP         : scripts/app_url.py --stop", res.get("token"))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
