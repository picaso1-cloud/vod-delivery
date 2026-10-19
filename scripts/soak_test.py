import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
from src import proxy

TEST_VIDEO = Path("/tmp/opencode/soak-source.mp4")


def ensure_test_video():
    if TEST_VIDEO.exists() and TEST_VIDEO.stat().st_size > 100_000:
        return TEST_VIDEO
    TEST_VIDEO.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
         "-c:a", "aac", "-b:a", "96k", "-shortest", str(TEST_VIDEO)],
        check=True, timeout=120)
    return TEST_VIDEO


class Reader:
    def __init__(self, url, name, pace_bps):
        self.url = url
        self.name = name
        self.pace_bps = pace_bps
        self.bytes_read = 0
        self.stall_events = 0
        self.task = None

    async def run(self, stop_at):
        import aiohttp
        deadline = time.monotonic() + stop_at
        buf = 0
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=20)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    t0 = time.monotonic()
                    async with sess.get(self.url) as r:
                        print(f"reader[{self.name}] headers after {time.monotonic()-t0:.2f}s status={r.status}", flush=True)
                        if r.status != 200:
                            body = await r.text()
                            print(f"reader[{self.name}] HTTP {r.status}: {body[:140]}", flush=True)
                            self.stall_events += 1
                            await asyncio.sleep(2)
                            continue
                        while time.monotonic() < deadline:
                            chunk = await r.content.read(256 * 1024)
                            if self.bytes_read == 0 and chunk:
                                print(f"reader[{self.name}] first body chunk after {time.monotonic()-t0:.2f}s len={len(chunk)}", flush=True)
                            if not chunk:
                                self.stall_events += 1
                                return
                            self.bytes_read += len(chunk)
                            buf += len(chunk)
                            now = time.monotonic()
                            if now - last_data > 1:
                                last_data = now
                            target_time = last_data + buf / self.pace_bps
                            if now < target_time:
                                await asyncio.sleep(target_time - now)
                                last_data = target_time
                                buf = 0
            except Exception as e:
                if self.stall_events < 5:
                    print(f"reader[{self.name}] error: {e}", flush=True)
                self.stall_events += 1
                await asyncio.sleep(2)


async def soak(minutes: float, pace_mbps: float):
    from aiohttp import web
    src = ensure_test_video()
    print(f"source: {src} ({src.stat().st_size // 1024}KB, looped infinitely)", flush=True)
    app = web.Application()
    app.router.add_route("*", "/stream/{token}", proxy.stream_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18091)
    await site.start()
    s = await proxy.start_virtual_session(str(src), "SOAK", transcode=False, start_offset=0.0, loop=True)
    print(f"session {s.token} primed at {time.strftime('%H:%M:%S')}", flush=True)
    s.clients = 1

    reader = Reader(f"http://127.0.0.1:18091/stream/{s.token}", "emu-tv", int(pace_mbps * 125_000))
    reader.task = asyncio.create_task(reader.run(minutes * 60 + 30))

    started = time.monotonic()
    samples = []
    last_served = 0
    last_sample = time.monotonic()
    worst_gap = 0.0
    respawns = 0
    try:
        while time.monotonic() - started < minutes * 60:
            await asyncio.sleep(5)
            now = time.monotonic()
            served = reader.bytes_read
            gap = now - last_sample if served == last_served else 0.0
            worst_gap = max(worst_gap, gap) if served == last_served else 0.0
            if served == last_served and now - last_sample > 5 and gap >= 5:
                pass
            last_served, last_sample = served, now
            dec_alive = s.proc is not None and s.proc.returncode is None
            if dec_alive is False:
                respawns += 1
            samples.append({
                "t": round(now - started, 1), "read_mb": round(served / 1048576, 1),
                "file_mb": round(s.path.stat().st_size / 1048576, 1),
                "pts": round(s.feed_content_s or 0, 1), "dec_alive": dec_alive,
            })
            elapsed = now - started
            if int(elapsed) % 15 < 5:
                print(json.dumps(samples[-1]), flush=True)
    finally:
        rate = reader.bytes_read / 125_000 / max(1e-9, time.monotonic() - started)
        print(json.dumps({
            "summary": True, "minutes": minutes,
            "read_mb": round(reader.bytes_read / 1048576, 1),
            "avg_mbps": round(rate, 2), "stall_events": reader.stall_events,
            "decoder_respawns_seen": respawns, "worst_read_gap_s": round(worst_gap, 1),
        }), flush=True)
        await proxy.stop_session(s.token)
        await runner.cleanup()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=1.5)
    ap.add_argument("--pace-mbps", type=float, default=4.0)
    args = ap.parse_args()
    await soak(args.minutes, args.pace_mbps)


if __name__ == "__main__":
    asyncio.run(main())
