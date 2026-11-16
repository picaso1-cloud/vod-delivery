# VOD Cast — Android client

Native Kotlin/Compose client for the VOD-delivery service. It searches the
catalog through the app API, prepares a signed stream link, and casts it to a
DLNA TV, a Chromecast, or plays it locally.

**Status:** v0.2.0 — verified on a Samsung SM-F946B (Android 13): launch, connect, async AI search,
Discover (AI picks + by-name + by-meaning), Live TV, library playback, TV list,
stream preparation, server-side casting and on-device playback. The UI uses a
dark cinematic theme with bottom navigation.

## Casting (v0.2.2)

- **Recovery:** the app restores the playing stream on launch (`GET /api/app/sessions`
  + persisted settings), so a crash, force-stop, network loss or just closing the app
  doesn't lose the Remote. A **Now playing** banner on Discover and the top-bar
  **Remote** button open the controls. Nothing playing? The app polls every 30 s and
  picks up a cast started elsewhere.
- **Pause/resume** works for sessions that have a TV attached (`/api/app/control`
  `pause|resume`); live sessions hide the seek buttons.
- The **Cast** button opens an in-app **Cast to** sheet with two sections:
  **Google Cast** (mDNS receivers, e.g. the TV box) and **Your TVs** (registered
  DLNA/Cast rows from the server, banned TVs excluded). Picking a TV sets it as
  the target; picking a Cast device connects the receiver. A registered TV that
  is also a Cast receiver is listed once, under Google Cast.
- Registered TVs (any row with a MAC, or `kind=cast`) are cast **server-side**
  through `POST /api/app/play` / `live/play`. The server owns the cast ladder,
  the wedge breaker, byte-truth verification and the TV's `downgrade` flag.
- Phone-direct DLNA (`SetAVTransportURI` from the app) is only used for
  renderers discovered locally over SSDP that are not registered.
- TVs with `downgrade=1` preselect the 1080p transcode in the play sheet.
- The **Cast** button in the top bar is a Google Cast route picker; a session
  must be chosen there before "Cast" can load a receiver.

## TV ban list

The TVs screen manages the server's MAC ban list: a **Ban** button on each TV,
a **Ban MAC** dialog (for devices not listed yet), and a **Banned devices**
section with **Unban**. Banned TVs never appear in the list, scans, the bot or
the web panel. The same list is editable from Telegram (`/bantv`, `/unbantv`)
and the web panel (TVs → Banned devices).


## Pairing (planned — next app build)

The server side is ready: the app will POST a pair request, the owner approves it in
Telegram or the web **App access** page, and the app polls once for its token. This
removes manual token entry. Full UX/API plan: [`docs/PAIRING.md`](../docs/PAIRING.md).

## Build (local only)

Requirements: JDK 17, Android SDK 35 / build-tools 35.0.0.

```bash
cd android
./gradlew clean :app:assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

The project is built on this machine; no CI service is used. Ready builds are
copied to `android/dist/` after each build (`vodcast-v0.2.apk` release,
`vodcast-debug.apk` debug). Both are signed with the local debug key, so they
install over each other.

If `./gradlew` is not executable: `chmod +x gradlew`.
Point Gradle at the SDK with `ANDROID_HOME` or `android/local.properties`:

```properties
sdk.dir=/path/to/Android/Sdk
```

## First run

1. Get the app token on the server:

   ```bash
   cd /home/souhail/VOD-delivery
   .venv/bin/python -c "from src import urlsign; print(urlsign.app_token())"
   ```

2. Open the app → enter both:
   - **LAN URL**: `http://192.168.1.240:8095`
   - **Public URL**: `https://vod.allocamera.com`

   The app probes the LAN URL first and falls back to the public one. The active
   route is shown in the top bar (`LAN` / `PUBLIC`); the gear icon reopens setup.
3. Paste the token → **Connect**.
4. **TVs** → pick a registered TV (or **Discover DLNA**), then search and play.

## API used (all query parameters, never a JSON body)

| Flow | Call |
|---|---|
| connect | `GET /api/app/config` |
| search | `GET /api/app/search?q=&type=movie\|series` |
| discover (3 groups) | `GET /api/app/discover?q=&kind=` → `ai_picks`, `by_name`, `by_meaning` |
| live categories | `GET /api/app/live/categories` → `[{id,name,count}]` |
| live channels | `GET /api/app/live?q=&category_id=` → `[{channel_id,title,icon}]` |
| live stream | `POST /api/app/live/stream?channel_id=` → `{token,lan_url,public_url,mime,mode:"live"}` |
| episodes | `GET /api/app/episodes?series_id=` → `items[season][].media_key` |
| list TVs | `GET /api/app/tvs` |
| TV capabilities | `GET /api/app/tvs/detail?name=` |
| probe methods | `POST /api/app/tvs/probe?name=` |
| prepare link | `POST /api/app/stream?media_key=&mode=cache&quality=copy&prepare=1[&format=mp4]` |
| library | `GET /api/app/library` · `POST /api/app/library/stream?media_key=` |
| watchlist | `GET /api/app/watchlist` · `POST /api/app/watchlist/{add,remove}?media_key=` |
| server cast | `POST /api/app/play?media_key=&tv=` · `POST /api/app/playback?action=` |
| session truth | `GET /api/app/session?token=` (`max_served` = bytes the TV really fetched) |
| seek / stop | `POST /api/app/control?token=&action=seek\|stop&value=` |

`POST /api/app/stream` accepts `prepare=1`: it returns `lan_url`/`public_url`
**without** starting the provider feed. The feed starts on the first client GET.

## Casting rules

- **DLNA**: SSDP `MediaRenderer` discovery, minimal DIDL-Lite
  `http-get:*:<mime>:DLNA.ORG_OP=01`, then `SetAVTransportURI` + `Play`.
  MKV only works on TVs that declare `video/x-matroska`/`video/x-mkv`; use the
  1080p transcode (MPEG-TS) otherwise.
- **Chromecast**: always request `format=mp4` (fragmented MP4). Google's default
  receiver does not play MPEG-TS. Pick the device with the Cast button in the
  top bar before starting.
- **Phone**: `quality=downgrade` (MPEG-TS H.264/AAC). MKV keeps its index at EOF
  and cannot be read progressively — the player would stall. The server waits for
  new bytes at the live edge instead of returning 416 while the feed is alive.

## Project layout

```text
android/app/src/main/java/com/souhail/vodcast/
  MainActivity.kt            Compose host
  data/Api.kt                OkHttp + kotlinx.serialization client
  data/Models.kt             API models
  data/Settings.kt           base URL + token + last TV
  cast/DlnaCaster.kt         SSDP + description + SOAP AVTransport
  cast/CastCaster.kt         Cast SDK + CastOptionsProvider
  player/PlayerActivity.kt   Media3/ExoPlayer ("play here")
  ui/App.kt                  Setup / Search / Detail / TVs / Play sheet / Remote
```

## Known gaps (next agent)

1. **Live TV on Chromecast** — the default receiver does not play MPEG-TS; live
   works on DLNA TVs and the phone. A custom Cast receiver (HLS/fMP4) fixes it.
2. **Pause/resume** — the app API exposes `stop` and `seek` only. Add
   `pause`/`resume` for server-side casts (`/api/app/play`) or DLNA control.
3. **Probe UI** — method badges are shown, but method selection is automatic
   (DLNA vs Cast by TV kind). Add a manual override.
4. **Session recovery** — if the server restarts, the session 404s; recreate it
   automatically and resume from the last position.
5. **Continue watching / watchlist** — endpoints exist; no screens yet.
6. **Roku / AirPlay / webOS / DIAL** — only DLNA and Cast are wired.
7. **Custom Cast receiver** — the default receiver is used (`CC1AD845`); a
   custom receiver enables MPEG-TS/HLS and subtitles.

## Logging

Every event is logged to three places: the Android log (tag **`VODCAST`**), a rolling
file (`files/logs/vodcast.log`, 1 MB × 2), and the in-app **Logs** screen (bug icon in
the top bar) with **Refresh / Share / Clear**.

- Logged: app start + device info, screen changes, searches, detail/live opens, TV
  select/probe, connect route, prepare/start/stop/seek, DLNA discovery + SOAP result,
  Cast load/stop, player open/state/isPlaying/errors, every API call (method, path,
  status, duration, bytes), user-visible notifications (toasts), and uncaught crashes.
- Secrets are redacted: signed URL `sig`/`exp`, `token` query values, bearer headers.
- Share exports the file through a FileProvider (chooser → Telegram/Drive/etc.).
- Filter live: `adb logcat -s VODCAST`.

## Troubleshooting

- **401** — token mismatch; re-copy `app_token`.
- **Connection refused at home** — use the server's LAN IP (see `/api/app/config`
  `lan_ip`), not `localhost`.
- **DLNA cast returns 500/UPnP error** — the container is unsupported; retry with
  the 1080p transcode.
- **Cast device not listed** — tap the Cast button; the phone and TV must be on
  the same network.
- **TV black screen** — check `/api/app/session` `max_served`; if it stays 0 the
  TV never fetched the URL (wrong LAN IP or DLNA renderer rejected the URI).

## Verified on device

- Samsung SM-F946B, Android 13 (SDK 33), over the public edge
  (`https://vod.allocamera.com`).
- Launch → connect → search (`greenland`, 8 results) → TV list (6 TVs) →
  prepare (3.1 s, public URL, `mode=cache format=ts mime=video/mpeg`) →
  playback: `c2.qti.avc.decoder` active, AudioTrack `state:started`, no
  `PlaybackException`.
- The app requires an **AppCompat theme** (`Theme.AppCompat.DayNight.NoActionBar`):
  `MediaRouteButton` reads AppCompat's `colorPrimary` and crashes with
  `background can not be translucent: #0` on a framework-only theme.
- Live TV verified server-side: 176 categories, 63 channels in the first
  category, `POST /api/app/live/stream` returns a signed `.ts` URL, the live
  feed served 256 KB with `Accept-Ranges: none` and stopped cleanly.
- Live TV verified on the device: BEIN Sports → `c2.qti.avc.decoder`
  initialized, `state READY`, `isPlaying=true`, frames flowing, no errors.
- Dead channels now fail fast: `{ok:false, error:"live source unavailable …"}`
  (verified with a 403 channel); the app shows that error instead of a dead URL.
- Live responses are **chunked** (no fake `Content-Length`); a fake 2 GB length
  made ExoPlayer's second connection wait 8 s for headers and time out.

## Search & app features

- **Async discover**: `by_name` / `by_meaning` render in <1 s (server ~0.2 s), AI picks arrive
  in a second call and are cached 10 min.
- **Filters**: minimum rating, from-year, language — applied server-side.
- **Continue watching** row with progress bars; resume position is passed to the player.
- **Detail**: "More like this" (similar titles), Watchlist add, version picker.
- **Library**: Downloads / Watchlist / Queue tabs, storage usage vs limit, cancel queue.
- **Player**: resume, speed (0.75–2×), audio/subtitle picker, PiP, 30-min sleep timer,
  progress saved every 10 s and on pause/stop.
- **Ops**: per-device app tokens, Prometheus `/metrics`, events feed,
  and a self-hosted client-log sink (`data/client_logs.jsonl`) for app errors.
