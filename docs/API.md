# App API (v1)

Authenticated JSON API for the Android casting client and future MCP bridge.
The Telegram flow is unchanged; this surface is additive.

Base URL (LAN): `http://<server>:8095`
Base URL (public): `https://vod.allocamera.com` (proxied to `/api/app/*` only)

## Authentication

Every route requires one of:

- `Authorization: Bearer <app_token>` — token is stored in the `settings` table under `app_token`
  (auto-generated on first use), or
- a request originating from localhost (`127.0.0.1` / `::1`).

Get the token on the server:

```bash
.venv/bin/python -c "import sys;sys.path.insert(0,'.');from src import urlsign;print(urlsign.app_token())"
```

Errors: `401` missing/invalid token, `422` invalid parameters, `503` bot bridge not ready.

## Media keys

| Kind | Format | Example |
|---|---|---|
| Movie | `movie:<stream_id>` | `movie:2652161` |
| Series | `series:<series_id>` | `series:69914` |
| Episode | `episode:<episode_id>:<series_id>` | `episode:2118240:69914` |

## Catalog

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/api/app/health` | – | `{ok, version, tvs, library, queue}` |
| GET | `/api/app/whoami` | – | `{ok:true}` (token check) |
| GET | `/api/app/search` | `q`, `type=movie\|series\|live` | array of groups with `variants[].media_key` |
| GET | `/api/app/episodes` | `series_id` | `{episodes:{season:[…]}}` |
| GET | `/api/app/live` | `q` (optional) | array of `{kind:"live", channel_id, title}` |

## Streaming (signed URLs)

| Method | Path | Params | Returns |
|---|---|---|---|
| POST | `/api/app/stream` | `media_key`, `start_position` (s), `mode=cache\|virtual`, `quality=copy\|downgrade` | `{ok, token, title, mime, duration, lan_url, public_url, path, expires_in}` |
| GET | `/api/app/session` | `token` | `{ok, mode, mime, position, duration, max_served, clients}` |
| POST | `/api/app/control` | `token`, `action=stop\|seek`, `value` (s) | `{ok, detail?}` |

- `mode=cache&quality=downgrade` → **recommended for phone/remote players**: MPEG-TS,
  transcoded as fast as the CPU allows (several × realtime), so players can fill their
  buffers. Responses end at the current file size (live-edge semantics) and ranges past
  EOF return `416`.
- `mode=virtual` → MPEG-TS at **1× realtime**. Used for TVs / server-controlled playback
  (screenshare pacing, server-side seek). Some players (e.g. MX Player) cannot fill their
  buffer from a realtime feed and will appear to buffer forever.
- `mode=cache&quality=copy` serves the original container (e.g. MKV). MKV puts its index
  at EOF, so progressive players may fail while the file is still growing — use for LAN
  TVs only.
- Signed URLs carry a container extension (`/s/{token}.ts`, `.mkv`) so players can
  identify the format without probing.
- `lan_url` works on the home network; `public_url` is present when the `public_base` setting is configured.
- `expires_in` is the signed-link TTL (6 h). The session ends after 15 min without a client fetch (3 min for non-link sessions).
- `max_served` is the byte-truth counter: a casting client should confirm TV consumption by watching it grow.

## Casting to a TV

| Method | Path | Params | Returns |
|---|---|---|---|
| POST | `/api/app/play` | `media_key`, `tv` (partial name), `start_position` | cast result |
| POST | `/api/app/live/play` | `channel_id`, `tv` | cast result |
| GET | `/api/app/status` | – | `{playing, tv, paused, position, diag}` |

`/api/app/play` runs the full delivery ladder (library → virtual → cache → proxy) and can take several minutes when a TV is unresponsive; call with a long client timeout.

## TVs

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/api/app/tvs` | – | array of `{name, ip, kind, prefer_proxy, downgrade, ignored}` |
| POST | `/api/app/tvs/scan` | `ip` | `{ok, kind, name}` or `{ok:false, notes}` |
| POST | `/api/app/tvs/wake` | `name` | `{ok, mac}` (Wake-on-LAN) |
| POST | `/api/app/tvs/flags` | `name`, `prefer_proxy`, `downgrade`, `v2_seek`, `virtual_channel` (0/1) | `{ok, tv}` |
| POST | `/api/app/tvs/rename` | `name`, `new_name` | `{ok, name}` |

## Library, queue, watchlist, history

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/api/app/library` | – | `{items:[…], usage_bytes}` |
| POST | `/api/app/library/delete` | `media_key` | `{ok}` |
| POST | `/api/app/library/archive` | `media_key` | `{ok, detail}` |
| POST | `/api/app/library/redownload` | `media_key` | `{ok}` (queues) |
| GET | `/api/app/queue` | – | `{items:[…]}` |
| POST | `/api/app/queue/cancel` | `media_key` | `{ok}` |
| GET | `/api/app/watchlist` | – | `{items:[…]}` |
| POST | `/api/app/watchlist/add` | `media_key` | `{ok, title}` |
| POST | `/api/app/watchlist/remove` | `media_key` | `{ok}` |
| GET | `/api/app/history` | `limit` (≤200) | `{items:[…]}` |
| GET | `/api/app/audit` | `limit` (≤100) | `{items:[…]}` (LLM auditor findings) |

Provider URLs are never returned by these endpoints.

## Typical client flow

1. `GET /api/app/search?q=resident%20evil` → pick `variants[].media_key`
2. `POST /api/app/stream?media_key=…&mode=virtual` → signed `lan_url`/`public_url`
3. Cast the URL with the best protocol for the TV (DLNA / Cast / SSAP / AirPlay)
4. Poll `GET /api/app/session?token=…` and require `max_served` to advance (byte truth)
5. `POST /api/app/control?token=…&action=seek&value=600` for server-side seek
6. `POST /api/app/control?token=…&action=stop` to free the provider slot

## Constraints

- The IPTV provider allows **one connection**: one active stream at a time (local or remote).
- Cached library content can be served to multiple clients concurrently (no provider slot).
- Sessions are in-memory: a server restart invalidates issued links.
- Each session is capped at `session_max_gb` (default 8 GB; virtual feeds 4 GB) and killed when exceeded.
- Starting a stream requires at least 10 GB free on the server; otherwise it fails with a `disk full` error after purging stale cache.
- The library is bounded by `cache_limit_gb` (default 50 GB); downloads are deferred while over budget.
