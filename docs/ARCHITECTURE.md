# Architecture

## Process layout

One Python process runs everything:

- aiogram bot (Telegram)
- aiohttp streaming proxy on `:8091`
- FastAPI web panel + JSON API on `:8095`

Shared state (provider sessions, active playback, per-chat sessions) lives in memory;
persistent state lives in SQLite (`data/vod.db`) and the vector index (`data/index.bin`).

```text
Telegram ──► bot (aiogram)
               │
               ├─ llm.parse_request ──► {intent, title, year, language, media_type, …}
               ├─ search (vector hits → SQL enrich → grouped, ranked variants)
               ├─ caster (DLNA AVTransport) / cc (Google Cast)
               └─ proxy (:8091) ── stream sessions (cache / virtual / proxy / HLS)
Web panel (:8095) ── HTML pages + /api/* (localhost) + /api/app/* (bearer token)
```

## Modules

| Module | Responsibility |
|---|---|
| `src/bot.py` | Telegram handlers, cast ladder, playback polling, app API bridge, library worker, audit worker |
| `src/proxy.py` | Stream sessions: provider downloader, ffmpeg, progressive serving, ranges, virtual channel, signed URLs |
| `src/caster.py` | DLNA SOAP: DIDL-Lite, SetAVTransportURI, Play, transport state, seek |
| `src/cc.py` | Google Cast: connect, media status, seek/pause |
| `src/ssdp.py` | SSDP discovery across all LAN interfaces |
| `src/scanner.py` | Identify/register TVs (Cast ports, DLNA descriptions, AVTransport validation) |
| `src/provider.py` | Xtream API client with disk-cached JSON catalogs |
| `src/store.py` | SQLite schema, migrations, queries |
| `src/search.py` | Vector hits → SQL rows → grouped/ranked results |
| `src/embedder.py` | sentence-transformers embeddings (384 dims, CPU) |
| `src/vector_index.py` | turbovec `IdMapIndex` wrapper |
| `src/indexer.py` | Catalog fetch → parse → SQLite → embed → index |
| `src/parsing.py` | Provider title/format parsing |
| `src/llm.py` | LLM request parsing + balanced-brace JSON extraction |
| `src/library.py` | Local cache library, download queue, watchlist expansion |
| `src/capabilities.py` | DLNA `GetProtocolInfo` parsing and format matching |
| `src/signing.py` / `src/urlsign.py` | HMAC-signed, expiring stream URLs |
| `src/web.py` | Panel + JSON API, app token auth |
| `src/mcp_server.py` | MCP bridge for external tooling |
| `src/auditor.py` | LLM self-audit over bot logs |

## Delivery modes (proxy)

| Mode | Container | Growth | Use |
|---|---|---|---|
| `cache` | original (e.g. MKV) | as fast as the provider allows | LAN TVs that can read progressive files |
| `cache_downgrade` | MPEG-TS | fast | TVs that need 1080p H.264 |
| `virtual` | MPEG-TS | 1× realtime | remote players, screenshare-style control, server-side seek |
| `proxy` | MPEG-TS pipe | realtime | short-lived relay to DLNA |
| `hls` | HLS | realtime | cast receivers that require HLS |

Clients include range requests and near-end probes; far-range probes receive a small
206 sliver so players can measure seekability without waiting for the full file.

## Casting ladder

Per TV, the bot tries, in order: `library → virtual → cache/downgrade → cache/copy →
proxy/downgrade → proxy/copy → direct`. Each attempt is verified by polling the TV's
transport state and by byte truth (`max_served`), never by the TV's self-report alone.

Per-TV flags in the `tvs` table: `prefer_proxy`, `downgrade`, `v2_seek`, `virtual_channel`.

## Signed streams

`/s/{token}?exp=&sig=` is HMAC-SHA256(secret, `token:exp`) with a 6-hour TTL.
Only `/s/*`, `/subs/*` and `/api/app/*` are meant to be exposed through a reverse proxy;
`/stream/` and the panel stay private.

## Constraints

- The IPTV provider allows **one concurrent connection**; sessions are serialized.
- Cached library files can serve multiple clients without a provider slot.
- Proxy sessions end after 15 minutes without a client fetch (3 minutes for non-link sessions).
- Session state is in memory: a restart invalidates issued links.
