# Android App — Clean-Room Spec

Derived from studying a legitimately purchased Web Video Cast 6.0.4 (build 8034).
Only **behaviour and protocol knowledge** is reused. No code, assets, branding, or
paid-feature bypass is copied. The app we ship is original code.

## 1. What WVC does (and what we take)

| WVC pattern | Our decision |
|---|---|
| Embedded browser detects video URLs on pages | **Reject** — our catalog is the source; no browser needed |
| Phone-side relay: `LocalFileServlet`, `MediaProxyServlet`, `CorsServlet`, `ThumbnailServlet`, `M3U8ToTSServlet` | **Reject** — `src/proxy.py` already is the relay, and a phone relay would fight the provider's single connection |
| ConnectSDK method matrix (DLNA/Cast/DIAL/Roku/AirPlay/webOS) | **Adopt the knowledge** — same probe order, our own implementation |
| Minimal DIDL-Lite: `http-get:*:<mime>:DLNA.ORG_OP=01`, `sec:CaptionInfoEx` + SMI/SRT resources | **Adopt** — matches `caster.py` |
| Cast `MediaInfo(url, streamType, contentType, metadata)` with `1=BUFFERED`, `2=LIVE` | **Adopt** |
| Web receiver page served to TVs/browsers (`/web-receiver/*`, `?deviceId=&label=&icon=`) | **Adopt as fallback** — a tiny `/tv` page on our public edge for TVs that cannot decode the stream natively |
| Subtitles served over HTTP by the app | **Reject** — server already serves `/subs/*` |
| Premium IAP (RevenueCat) gating history/queue/resume | **Reject** — our app has no paywall; features come from the API |
| Multicast permission + cleartext network config | **Adopt** — required for SSDP and HTTP media URLs |

## 2. Screens and flow

```text
[Setup]  base URL (LAN/public) + app token  ──►  [Home]
[Home]   search bar, watchlist, continue-watching, TVs
[Search] results grid (poster, year, lang variants)
[Detail] movie: play sheet · series: seasons → episodes
[Play]   stream sheet:
           URL:  ( ) LAN   ( ) Public      (only if public_url present)
           Mode: ( ) TV (virtual)  ( ) Player (cache_downgrade)
           Quality: Auto / Copy / 1080p
           [ Cast ]  [ Play here ]
[TVs]    discovered list (server + local SSDP merged by IP/MAC), probe status badge
[Remote] play/pause, ±30/60 s, seek bar, stop, "actually playing" = max_served Δ
```

**Flow**
1. App calls `GET /api/app/config` → knows `lan_ip`, `public_base`, version.
2. Search → `GET /api/app/search?q=&type=`; series → `GET /api/app/episodes`.
3. User opens Play sheet → `POST /api/app/stream?prepare=1` (URLs only, feed starts on first GET).
4. User picks LAN/Public + mode/quality → app casts the chosen URL.
5. Remote polls `GET /api/app/session` every 2 s; position + `max_served`.
6. Seek/pause/stop → `POST /api/app/control`.
7. Session gone (404 / `session ended`) → recreate via `POST /api/app/stream`.

## 3. Cast method matrix (probe order)

Probe once per TV, persist, never hammer.

| # | Method | Port / endpoint | Verify |
|---|---|---|---|
| 1 | DLNA | SSDP `urn:schemas-upnp-org:device:MediaRenderer:1` → description → AVTransport | `SetAVTransportURI` + `Play`, then any bytes in `max_served` |
| 2 | Cast v2 | TCP 8009 handshake, `MediaInfo` | receiver LOAD + `max_served` |
| 3 | DIAL | `:8008/ssdp/device-desc.xml`, `/apps/<app>` | app launch session |
| 4 | Roku ECP | `:8060/launch/<appId>` | `query/device-info` |
| 5 | AirPlay | `:7000/play` (bplist, SRP6) | `/rate` non-zero |
| 6 | webOS SSAP | `ws://:3000` / `wss://:3001` | `ssap://system.launcher/launch` |

Rules: one attempt per method per session; abort after 2 control-plane timeouts with a
power-cycle message (LIFEMAXX lesson); byte truth (`max_served`) is the only success signal.

## 4. Server gaps to add

| Endpoint / change | Purpose |
|---|---|
| `GET /api/app/config` | `lan_ip`, `public_base`, `version`, flags, server name |
| `GET /api/app/tvs/{name}` | TV row + `sink_protocols` + ranked methods + last probe time |
| `POST /api/app/tvs/probe` | run the matrix server-side, persist per-TV methods |
| `POST /api/app/stream?prepare=1` | return token + URLs without starting the feed |
| `POST /api/app/stream&format=mp4` | fMP4 for stock Chromecast (TS stays for our TCL) |
| `GET /api/app/episodes` (richer) | `{season, episode, title, media_key, duration}` |
| `GET /tv/{token}` | minimal receiver page (fallback for browser TVs) |

## 5. App module layout

```text
android/
  app/src/main/java/com/souhail/vodcast/
    MainActivity.kt              Compose host + nav
    ui/SetupScreen.kt            base URL + token
    ui/SearchScreen.kt           search + results grid
    ui/DetailScreen.kt           variants / seasons / episodes
    ui/PlaySheet.kt              LAN|Public, mode, quality
    ui/TvScreen.kt               discovered TVs + probe badges
    ui/RemoteScreen.kt           transport + max_served
    data/Api.kt                  Retrofit + kotlinx.serialization
    data/Settings.kt             DataStore (base, token, last TV)
    cast/DlnaCaster.kt           UPnPCast (MIT) — DIDL OP=01, subs
    cast/CastCaster.kt           play-services-cast MediaInfo
    cast/MethodProbe.kt          local SSDP + probe cache
    player/PlayerActivity.kt     Media3/ExoPlayer (play-here)
```

Dependencies: Compose (BOM), OkHttp/Retrofit, kotlinx.serialization, UPnPCast (MIT),
`play-services-cast`, Media3, DataStore. minSdk 26, target 35.

## 6. Build and distribution

No Android SDK on the server. Recommended: **GitHub Actions** builds a debug APK on
every push (`android.yml`) and uploads it as an artifact; the APK is installed from the
artifact page. A release keystore can be added later for a signed install.

## 7. Phases

- **P0** — setup, search, detail/episodes, TV picker (server list), stream sheet,
  DLNA cast, remote, session recovery.
- **P1** — Cast/Chromecast (`format=mp4`), local SSDP discovery, probe badges.
- **P2** — public URL parity, resume at position, watchlist/continue-watching.
- **P3** — `/tv` receiver fallback, AirPlay/webOS/DIAL/Roku, ABR.

## 8. Legal boundary

Studying a purchased app and reimplementing its **functionality** is one thing;
redistributing a modified APK or bypassing its paid features is another. This app is
written from scratch, ships no WVC code/assets/branding, and has no paywall to bypass.
