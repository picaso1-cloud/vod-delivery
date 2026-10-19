# Project Summary

## What this is

A self-hosted movie & series delivery system for a household: it indexes an
Xtream IPTV subscription, finds titles from natural language (Telegram or app),
and plays them on DLNA/Google Cast TVs, phones, and remote clients — with a local
cache library, live TV, a web panel, and a signed-URL stream API.

## Architecture

```text
Telegram (aiogram)                    Web panel / app (FastAPI :8095)
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
             search (LLM parse → turbovec → SQLite)
                       │
        ┌──────────────┼─────────────────┐
        ▼              ▼                 ▼
  caster (DLNA)   cc (Cast)       proxy (:8091)
                                        ├─ cache (download → growing file)
                                        ├─ cache_downgrade (fast MPEG-TS)
                                        ├─ virtual (1× realtime MPEG-TS)
                                        └─ /s/{token}.ts?exp&sig  (signed)
```

Public access: Caddy on a VPS + Tailscale to the home box. Only `/s/*`, `/subs/*`
and `/api/app/*` are exposed; the panel and the unsigned `/stream/` route stay private.

## Features

- **Natural-language search**: local LLM parses intent; ~116K titles embedded with
  `paraphrase-multilingual-MiniLM-L12-v2` (384 dims) and stored in a turbovec `IdMapIndex`.
- **Casting**: DLNA AVTransport ladder (library → virtual → cache → proxy → direct),
  Google Cast, per-TV flags (`prefer_proxy`, `downgrade`, `v2_seek`, `virtual_channel`).
- **Streaming modes**: progressive cache (raw container), fast transcoded MPEG-TS,
  realtime virtual TS (server-side seek via decoder swap), HLS.
- **Signed URLs**: HMAC-SHA256, 6 h TTL, LAN + public; signed path carries the
  container extension.
- **App API**: 27 bearer-authenticated endpoints (search, episodes, live, stream,
  session, control, play, TVs, library, queue, watchlist, history, audit).
- **Library**: download queue, watchlist episode buffering, disk budget.
- **Live TV**: browse/search/play via the same local-feed architecture.
- **Self-audit agent**: LLM reviews logs every ~10 min and pushes HIGH findings.
- **Web panel**: dashboard, TVs, history, watchlist, library, settings.

## Verified end-to-end

- LIFEMAXX (DLNA/GStreamer): virtual channel played, byte-truth `max_served` advancing.
- MX Player (phone, mobile data): `cache_downgrade` TS through the public edge,
  309 MB consumed, ~9 minutes in, no stalls.
- VLC: public signed URL playback.
- Range/206 through Caddy, ~10 MB/s throughput, instant near-end probe answers.
- 57 unit tests, infrastructure-free.

## Hard constraints

- IPTV provider: **one concurrent connection**, kills long streams, requires the
  `SanSatV4Player` user agent.
- MKV keeps its index at EOF → not progressively seekable while growing.
- DLNA TVs differ wildly (Samsung wedges, LIFEMAXX probes aggressively).
- Sessions are in-memory: restarting the process invalidates signed links.

## Safety & hardening (current)

- Per-session cap `session_max_gb` (default 8 GB; virtual 4 GB), watchdog every 30 s.
- 10 GB free-space guard before a stream; stale cache purged first.
- Library budget `cache_limit_gb` (default 50 GB); downloads defer when over.
- Low-disk Telegram alert (< 15 GB free).
- Startup wipe of `data/cache/streams/`; kill/idle-reap deletes session files.
- Signed URLs + bearer token; localhost bypass; `/stream/` restricted to LAN,
  loopback and Tailscale (100.64/10) sources.
- Provider credentials redacted from logs; `httpx` request logging disabled.
- Log rotation at 50 MB × 3 (`/etc/logrotate.d/vod-delivery`).

## Known gaps / roadmap

1. **Native Android app** — API is ready; ConnectSDK/jUPnP + per-TV method tester.
2. **Adaptive bitrate ladder** — start 240p, step up every 10–20 s via decoder swap;
   measure with `max_served` deltas. (See ROADMAP.md.)
3. **AirPlay / webOS SSAP** — the webOS TV and possibly LIFEMAXX remain unreachable.
4. **Multi-stream arbitration** — provider allows one stream; library cache is the
   only concurrent path. Options: queue, second subscription, or VPS-direct pull.
5. **Session persistence** — links die on restart; could persist session metadata
   (media key, resume position) and auto-resume after boot.
6. **Public API hardening** — rate limiting and per-token scopes on `/api/app/*`.
7. **Observability** — per-stream metrics (bitrate, stalls, respawns) beyond logs.
8. **Cast-session security** — inherent to DLNA/Cast; rely on network segmentation.

## Repository layout

```text
src/        bot, proxy, caster, cc, ssdp, scanner, provider, store, search,
            embedder, vector_index, indexer, parsing, llm, library, web,
            capabilities, signing, urlsign, auditor, mcp_server
tests/      57 unit tests
scripts/    indexer helpers, backup, soak/probe/phase2/app_url tooling
deploy/     systemd units + logrotate
docs/       SETUP.md, ARCHITECTURE.md, API.md, PROJECT_SUMMARY.md
```
