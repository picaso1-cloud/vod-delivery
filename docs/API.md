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
| GET | `/api/app/health` | – | provider/sessions/library/uptime + counts |
| GET | `/api/app/whoami` | – | `{ok:true}` (token check) |
| GET | `/api/app/config` | – | `{ok, version, lan_ip, public_base, has_public, server_name}` |
| GET | `/api/app/search` | `q`, `type=movie\|series\|live` | array of groups with `variants[].media_key` |
| GET | `/api/app/discover` | `q`, `kind`, `year_from`, `year_to`, `rating_min`, `lang`, `genre` | fast groups: `{by_name, by_meaning, ai_pending:true}` |
| GET | `/api/app/discover/ai` | same filters | `{ai_note, ai_picks:[group+reason]}` (LLM, cached 10 min) |
| GET | `/api/app/similar` | `media_key` | `{title, items:[group]}` |
| GET | `/api/app/continue` | – | `{items:[{media_key,title,poster,position,duration}]}` |
| GET/POST | `/api/app/progress` | `media_key` (+ `position`,`duration` POST) | `{position,duration}` |
| GET | `/api/app/events` | `since` | `{items:[{id,ts,kind,title,body}]}` |
| GET/POST/DELETE | `/api/app/tokens` | `label` / `token` | per-device tokens (panel session or localhost only) |
| POST | `/api/app/pair/request` | JSON `{device_name, platform, instance_id}` | `{request_id, code, expires_in, notified}` (no auth, 6/min/IP) |
| GET | `/api/app/pair/status` | `request_id`, `code` | `{status, token?}` — token returned once when approved |
| POST | `/api/app/client-log` | `level`,`tag`,`message` | self-hosted app error sink |
| GET | `/metrics` | – | Prometheus text (requests, sessions, library, provider) |
| GET | `/api/app/episodes` | `series_id` | `{episodes:{season:[…]}, items:{season:[{episode,title,media_key,ext,duration}]}, series_id}` |
| GET | `/api/app/live` | `q` (optional), `category_id` (optional) | array of `{kind:"live", channel_id, title, category_id, icon}` |
| GET | `/api/app/live/categories` | – | `[{id, name, count}]` |

## Device pairing (app access)

The app asks for access without a token; the owner approves; the device collects a token once.

1. `POST /api/app/pair/request` with `{"device_name","platform","instance_id"}` → `{request_id, code, expires_in, notified}` (`notified` says whether the Telegram owner was reached).
2. The owner approves in Telegram (inline buttons) or the web panel **App access** page (`/tokens`).
3. `GET /api/app/pair/status?request_id=&code=` → `pending` → `approved` + `token` (once) → `claimed`.
4. The app stores the token and sends `Authorization: Bearer <token>`.

Limits: 6 requests/min/IP, 20 pending requests, 10-minute TTL. A wrong `code` returns 404. Denied/expired requests never return a token.

Admin paths: web **App access** (`/tokens`, panel password) and Telegram `/token` / `/tokens`. The `/api/app/tokens` endpoints are admin-only (panel session or localhost); the panel and bot use the store directly.

## TVs and cast methods

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/api/app/tvs` | – | `[{name, ip, mac, kind, prefer_proxy, downgrade}]` |
| GET | `/api/app/tvs/detail` | `name` | `{ok, tv, protocols, methods:[{method,available,detail,verified_at}], last_probe}` |
| POST | `/api/app/tvs/probe` | `name` | same shape — runs SSDP/description and port probes for cast/dial/roku/airplay/webos |
| GET | `/api/app/tvs/banned` | – | `{ok, banned:[{mac,label,created_at}], macs, ips, names}` |
| POST | `/api/app/tvs/ban` | `mac`, `label` | `{ok, mac}` — hides the device everywhere |
| POST | `/api/app/tvs/unban` | `mac` | `{ok, mac}` |

All `POST` parameters are **query parameters** (`?key=value`), not a form or JSON body.

## Streaming (signed URLs)

| Method | Path | Params | Returns |
|---|---|---|---|
| POST | `/api/app/stream` | `media_key`, `start_position` (s), `mode=cache\|virtual`, `quality=copy\|downgrade`, `prepare=1`, `format=mp4`, `declare_current=0\|1` | `{ok, token, title, mime, duration, mode, format, prepared, lan_url, public_url, path, expires_in}` |
| GET | `/api/app/session` | `token` | `{ok, mode, mime, position, duration, max_served, clients}` |
| POST | `/api/app/control` | `token`, `action=stop\|seek`, `value` (s) | `{ok, detail?}` |
| POST | `/api/app/playback` | `action=pause\|resume\|seek\|stop`, `value` | `{ok, ...}` — controls the server-side cast |
| POST | `/api/app/library/stream` | `media_key` | signed stream for a downloaded file (`mode:"library"`) |
| POST | `/api/app/live/stream` | `channel_id` | `{ok, token, title, mime:"video/mpeg", mode:"live", duration:0, lan_url, public_url, expires_in}` |

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
- `prepare=1` returns token + `lan_url`/`public_url` **without starting the feed**; the
  downloader starts on the first client GET. Use it while the user is still choosing.
- `format=mp4` (with `mode=cache`) produces fragmented MP4 — required by the stock
  Chromecast receiver; MPEG-TS sessions are not server-seekable.
- `declare_current=1` (default) declares the **current** file size. While the feed is
  alive, a range request at the live edge blocks until new bytes arrive (up to 30 s)
  and only returns `416` after the download completes — this keeps ExoPlayer and other
  progressive players streaming a growing file.
- `declare_current=0` declares the probed true total; use it when the client needs a
  stable duration/size before playback.

### Live TV

`POST /api/app/live/stream` starts a **live-file** session: ffmpeg appends the channel
to a growing TS file and the TV/player is served that file as a **chunked** response
(`Accept-Ranges: none`, no `Content-Length`) — a fixed fake length breaks ExoPlayer's
second connection. Dead channels return `{ok:false, error:"live source unavailable …"}`
after ~7 s instead of a URL that 404s. Live sessions are non-seekable, `duration` is 0,
and `max_served` stays 0 — use `clients` and the server log to confirm delivery. The provider's live slot is released
when the session is stopped or the idle reaper fires.

## Receiver fallback page

`GET /tv/{token}?exp=&sig=&ext=ts|mkv|mp4` returns a minimal HTML5 `<video>` page that
plays the signed URL. Exposed on the public edge (`/tv/*`) for TVs that cannot decode
the stream natively. It does not require the bearer token — the signed URL is the
credential.
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
| GET | `/api/app/tvs` | – | array of `{name, ip, mac, kind, prefer_proxy, downgrade, ignored}` |
| POST | `/api/app/tvs/scan` | `ip` | `{ok, kind, name}` or `{ok:false, notes}` (banned MACs are refused) |
| POST | `/api/app/tvs/wake` | `name` | `{ok, mac}` (Wake-on-LAN) |
| POST | `/api/app/tvs/flags` | `name`, `prefer_proxy`, `downgrade`, `v2_seek`, `virtual_channel` (0/1) | `{ok, tv}` |
| POST | `/api/app/tvs/rename` | `name`, `new_name` | `{ok, name}` |
| GET | `/api/app/tvs/banned` | – | `{ok, banned:[{mac,label,created_at}], macs, ips, names}` |
| POST | `/api/app/tvs/ban` | `mac`, `label` | `{ok, mac}` — MAC ban list, hidden from every TV surface |
| POST | `/api/app/tvs/unban` | `mac` | `{ok, mac}` |

## Updates (OTA)

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/api/app/update` | `current=<versionCode>` | `{ok, version_name, version_code, size, notes, available}` |
| GET | `/api/app/update/apk` | – | the newest APK (`application/vnd.android.package-archive`) |

The panel serves `android/dist/vodcast-v<version>.apk`, where the version is read
from `android/app/build.gradle.kts`. If the file is missing it tries
`gh release download v<version>` (5-minute negative cache). Release notes are
read from `android/release-notes/v<version>.md`. The client downloads with its
token header, so the endpoint works over LAN and through the public base.

## Tracks (audio / subtitles)

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/api/app/tracks` | `media_key`, `force=0\|1` | `{ok, audio:[{index,codec,lang,title,channels,default}], subs:[{index,codec,lang,title,forced,default}], probed_at}` |

The probe runs on first use and is cached in `media_tracks`; it is deferred (returns `deferred:true`) while a provider session is active because the account allows one connection. `force=1` re-probes.

Pass the chosen tracks to playback:

- `POST /api/app/stream?...&audio=<index>&sub=<index>` — `audio` switches copy-quality casts to a remux (`cache_remux`, MKV) that drops the unused tracks.
- `POST /api/app/play?...&audio=<index>&sub=<index>` — server-side cast uses the same mapping through the normal ladder.
- `-1` (default) keeps the file's default streams.

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

### Discover (3 result groups)

`GET /api/app/discover` returns three curated groups for a natural-language query:

- **`ai_picks`** — the LLM curator ranks up to 6 catalog titles from the semantic +
  keyword candidate pool and returns a one-line reason for each. Cached for 10 min.
- **`by_name`** — SQL keyword matches on titles (and series plot/genre), with simple
  plural stemming (`zombies` also matches `zombie`).
- **`by_meaning`** — vector (semantic) matches.

Items already in `ai_picks` are removed from the other two groups so the three
sections never repeat a title. If the LLM is unavailable, `ai_picks` falls back to
the top-rated candidates and `ai_note` says so.
