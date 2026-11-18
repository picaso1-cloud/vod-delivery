# VOD Delivery

A self-hosted movie & series delivery stack: it indexes an Xtream IPTV subscription, finds titles from natural-language Telegram messages, and plays them on DLNA / Google Cast TVs — with a local cache library, live TV, a web panel, and a signed-URL stream API for phones and remote viewing.

## What it does

- **Natural-language search** in Telegram (`resident evil`, `the wire s2e3`, `something funny from 2019`) — parsed by a local LLM, resolved against a vector index of the provider catalog.
- **Casts to TVs** over DLNA (UPnP AVTransport) and Google Cast, with per-TV delivery strategies (progressive cache, virtual MPEG-TS feed, transcoding for older panels).
- **Server-side streaming** with resume, seeking, and byte-truth verification (it checks what the TV actually fetched, not what it claims).
- **Local cache library** with a download queue, watchlist episode buffering, and disk-budget management.
- **Live TV** (browse, search, play) using the same local-feed architecture.
- **Web panel** (FastAPI) for TVs, history, watchlist, library, and settings.
- **Signed stream URLs** (`/s/{token}?exp=…&sig=…`) for remote/mobile playback — works over LAN or a public HTTPS edge.
- **LLM self-audit agent** that reviews bot logs and files findings for review.

## Architecture

```text
Telegram ──► bot (aiogram) ──► search (LLM parse → vector index → SQLite)
                                 │
                                 ├─► caster (DLNA SOAP) / cc (Google Cast)
                                 └─► proxy (aiohttp :8091)
                                        ├─ cache sessions (download → progressive file)
                                        ├─ virtual sessions (ffmpeg → MPEG-TS feed)
                                        └─ signed URLs (/s/…)
Web panel :8095 ── JSON API (/api/*, /api/app/*)
```

The IPTV provider allows **one concurrent connection**; the proxy serializes sessions and the library cache allows multi-client playback without a provider slot.

## Requirements

- Python 3.12
- ffmpeg (`/usr/bin/ffmpeg`) and ffprobe
- An Xtream Codes-compatible IPTV subscription
- Optional: a local OpenAI-compatible LLM endpoint for request parsing (default `http://localhost:8086/v1`)

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env       # fill BOT_TOKEN and XSTREAM_* credentials

.venv/bin/python -m src.indexer   # build data/vod.db + vector index (first run ~minutes)
.venv/bin/python -m src.bot       # start the bot (web panel on :8095)
```

The first Telegram `/start` claims ownership when the allowlist is empty.

## Regenerate from scratch (one stop)

Everything needed to rebuild the stack is documented in **[docs/SETUP.md](docs/SETUP.md)**:

```bash
./scripts/bootstrap.sh     # venv + dependencies + .env template
make index                 # provider catalogs → SQLite → embeddings → turbovec index
make run                   # bot + web panel + stream proxy
```

`docs/SETUP.md` covers the provider catalog fetch, title parsing, SQLite schema,
the `paraphrase-multilingual-MiniLM-L12-v2` embedding pipeline (384 dims, CPU),
the turbovec `IdMapIndex` build, the llama.cpp LLM configuration, systemd deployment,
verification, and troubleshooting.

## Documentation

- [docs/PROJECT_SUMMARY.md](docs/PROJECT_SUMMARY.md) — what the system does, verified behavior, constraints, known gaps
- [docs/SETUP.md](docs/SETUP.md) — installation and full regeneration
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — modules, delivery modes, casting ladder, constraints
- [docs/API.md](docs/API.md) — authenticated app API (search, streams, casting, TVs, library)

## Deployment

`deploy/` contains systemd units:

- `vod-delivery.service` — the bot + panel
- `vod-indexer.service` + `vod-indexer.timer` — weekly catalog re-index
- `vod-backup.*`, `vod-catalog-drift.*` — optional maintenance jobs

```bash
sudo cp deploy/vod-delivery.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vod-delivery.service
```

## API

See [docs/API.md](docs/API.md) for the authenticated app API: search, episodes, live channels, signed streams, sessions, casting, TVs, library, queue, watchlist, history, and audit findings.

Highlights:

- `POST /api/app/stream` → `{lan_url, public_url, duration, expires_in}`
- `GET /api/app/session?token=…` → `max_served` (byte-truth consumption)
- `POST /api/app/play` → full cast ladder to a TV

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Security notes

- `.env` holds the Telegram token and provider credentials — never commit it.
- The web panel is password-protected (default `admin`; change it in Settings).
- The public API is bearer-token protected (`app_token` in the settings table); stream links are HMAC-signed and expire.
- Do not expose the panel (`:8095`) or the unsigned `/stream/` route to the internet; expose only `/api/app/*` and `/s/*` through a reverse proxy.
- TV discovery and DLNA casting operate on the local network only.

## Android client

`android/` is a native Kotlin/Compose client (`com.souhail.vodcast`): AI discover,
Live TV, library/queue/watchlist, on-device playback, and casting through the
server. Builds are local (JDK 17, Android SDK 35, Gradle 8.9):

```bash
cd android
ANDROID_HOME=/opt/android/sdk ./gradlew :app:assembleRelease
# APKs land in android/dist/ (vodcast-v0.2.apk, vodcast-debug.apk)
```

Casting rules the app follows:

- Registered TVs (anything with a MAC, or `kind=cast`) are cast **server-side**
  via `POST /api/app/play` — the server's ladder, wedge breaker and byte-truth
  checks are what actually keep a TV playing. Phone-direct DLNA is only used
  for renderers found by local SSDP that are not registered.
- TVs with `downgrade=1` (4K-HEVC panels) default to the 1080p transcode.
- Banned TVs (MAC ban list) are hidden from the TV screen and local discovery.

## Changelog

### 0.3.5 — 2026-11-18

- **Arabic-aware search**: `normalize_arabic` (أ/إ/آ→ا, ة→ه, ى→ي, tatweel and
  diacritics) plus definite-article stripping for both the index and query
  tokens, so `القصة الكاملة` also matches `قصه كامله`.
- **Native titles are indexed**: `search_fts` now stores
  `title + native_title + article-stripped variants`; before, Arabic names of
  English-titled shows were invisible to name search. Results and the app's
  detail/cards show the native title under the English one (`native_title` in
  `/api/app/discover`).
- **Parser fix**: provider tags (`V.SHAHID`, `V.FORJA`, …) are skipped and only
  the first non-title/non-year part is kept as `native_title`; 256 existing
  series were repaired in place.
- **Index health**: the FTS is rebuilt automatically when its row count differs
  from the catalog (2,763 titles were missing), and the bm25 contribution is
  capped at 40 so plot hits can't outrank a real title match.
- Verified: `القصة الكاملة` returns all three anthology entries first;
  `جريمة في الزمالك`, `بني مزار` and `لعبة جهنم` each resolve to the right show.

### 0.3.4 — 2026-11-18

- **In-app updates (OTA)**: the client asks the server for the newest build on
  launch (max once per 6h), shows the release notes, downloads the APK with
  progress and opens the installer. `GET /api/app/update` returns
  version/code/size/notes; `GET /api/app/update/apk` serves the file. The panel
  reads `android/dist/vodcast-v<version>.apk` (with the version taken from
  `build.gradle.kts`) and falls back to `gh release download` when the file is
  missing, so a fresh server heals itself from GitHub releases. Notes come from
  `android/release-notes/v<version>.md`. "Later" skips that version.
- **Several LAN URLs**: the setup field accepts a comma-separated list
  (`http://192.168.1.240:8095, http://192.168.31.106:8095`); the app tries each
  in order, then the public URL. Fixes "won't connect" when the phone is on the
  other home subnet.
- **Tokens are additive**: minting a token never disables existing devices. The
  legacy shared token is migrated into the token table on boot so it now shows
  in *App access* and can be revoked like any other.

### 0.3.3 — 2026-11-17

- **Series language picker**: the detail screen lists every stream of a series
  with its language and real episode count (`English · 40 eps`,
  `English · 30 eps`, `French · 37 eps`, …) and starts on the most complete
  one, so a stream with a half-empty final season is no longer the only choice.
  Unreachable streams are marked `unavailable`. Search cards show every
  language (`2022 · Series · EN · FR`).
- **Same-language duplicates are kept for series**: the catalog carries the same
  show several times per language with different completeness (FROM: EN 30 eps
  vs EN 40 eps, FR 22/32/37). Only one was kept before, and it happened to be an
  incomplete one. Movies still dedupe per language.
- Telegram: multi-stream series now get one "📚 <language>" button per stream
  instead of a single "Browse episodes" that always picked the first.
- Merged search results union their variants instead of keeping only the first
  group's list.
- **webOS (LG DMRplus) casting fixed**: plain-DLNA TVs now start with a
  file-mode MKV remux (`cache_mkv`) instead of piping a live TS. The webOS DMR
  accepts a real file with byte ranges and starts in ~2 s; the previous direct
  provider URL returned 716 and the chunked pipe made `Play` time out after 30 s
  and wedged the stack. Verified end-to-end: `PLAYING`, 68 MB served.

### 0.3.2 — 2026-11-17

- **Fix: titles that are English function words were unfindable.** "from",
  "it", "you", "them" were dropped by the search stopword filter, so the name
  lookup returned nothing and only meaningless vector neighbours were shown.
  Now the stopword filter falls back to the raw words, exact-title matching also
  strips a trailing `(YYYY)` and trailing `movie/series/show/tv`, and a
  dedicated exact-title pass runs for every query. `FROM (2022)` is the first
  result again.
- When a query has no semantic signal (all stopwords, e.g. "from"), the app
  skips vector/AI suggestions entirely — no more unrelated Arabic shows under
  "By meaning" and no AI picks built from them.
- The Telegram bot uses the same merged name+semantic search, and rescues
  messages the LLM classified as smalltalk/status when the text is exactly a
  catalog title ("from" used to get "I'm your media butler").
- The app re-runs the search when you switch Movies/Shows/Live TV, instead of
  clearing the results and waiting for another tap on Search.

### 0.3.1 — 2026-11-16

- **Fix: failed casts poisoned the provider.** When a cache session never
  produced bytes, the abort path returned without stopping it, so the ffmpeg
  reader kept the single provider connection and every retry failed with
  "provider busy or down". The abort now stops the session, and the feed marks
  its session ready on the first chunk so a healthy cast starts immediately.
- **Audio picker is usable**: a vertical list of every track with full language
  names (German, English, Spanish, …), codec/channels and the default marker;
  the dialog scrolls when there are many tracks. Subtitle languages are listed
  the same way.
- Probing an empty/failed file is cached for 1 h so reopening the sheet does not
  re-probe slowly every time.
- Remote controls no longer overflow (Stop wrapped its label).

### 0.3.0 — 2026-11-16

- **Track discovery**: the server now ffprobes a stream on first use and stores
  its real tracks (`media_tracks`), because the provider catalog only labels one
  language per row. MULTI rows can carry 5-6 audio languages and up to 10
  subtitle tracks.
- **Audio selection**: the play sheet lists the file's audio tracks (language,
  codec, channels) and the cast uses the chosen one (`-map 0:v:0 -map 0:<idx>`).
  Copy-quality casts switch to a remux (`cache_remux`, MKV) so the extra tracks
  are dropped without re-encoding; transcode/TS paths map the same track.
  Telegram: `/audio` lists tracks and `/audio <n>` re-casts at the current
  position.
- **Subtitle visibility**: the play sheet shows which subtitles are embedded
  (language + forced). Picking/burning them is the next phase.
- API: `GET /api/app/tracks?media_key=`, `audio`/`sub` params on
  `POST /api/app/stream` and `/api/app/play`.

### 0.2.2 — 2026-11-16

- **Cast state survives app restarts.** New `GET /api/app/sessions` lists the
  active proxy sessions; the app restores the playing stream (server cast or
  app session) on launch, so a crash, a force-stop, losing the network or just
  closing the app no longer loses control.
- **"Now playing" banner** on Discover (title + **Control**) plus the top-bar
  **Remote** button, both driven by the restored session. The app also polls
  every 30 s while nothing is playing, so a cast started elsewhere (e.g.
  Telegram) appears without restarting the app.
- The last selected TV is restored on launch (`settings.lastTv`), so the play
  sheet works immediately.
- `/api/app/stream` accepts `tv=` and stores it on the session;
  `/api/app/control` supports `pause|resume` for sessions that have a TV
  attached (server-side DLNA/Cast pause). Live sessions hide the seek buttons.
- `/api/app/stream` and `live/stream` now report `live`.

### 0.2.1 — 2026-11-16

- **One cast picker in the app.** The Cast icon now opens an in-app sheet with two
  sections: **Google Cast** (mDNS receivers) and **Your TVs** (registered DLNA/Cast
  rows, banned TVs excluded). Picking a TV sets it as the target; picking a Cast
  device opens the receiver session. Cast devices that are also registered as TVs
  are shown once, under Google Cast.
- Cast requests now run on an app-level coroutine, so dismissing the play sheet
  no longer cancels an in-flight `/api/app/play`.

### 0.2.0 — 2026-11-16

- **Fix: casts dying after 20-30 s.** The cast ladder never broke out on success:
  it stopped the session that was serving a PLAYING TV, then re-cast the next
  mode 30 s later. The TV played only its initial buffer and went black. The
  ladder now breaks with the winning mode and keeps that session.
- **Fix: Cast button crash** (`MainActivity` must extend `FragmentActivity`
  for `MediaRouteButton`'s route dialog).
- **Fix: search HTTP 500** after the weekly index rebuild — the running process
  kept the old vector index in memory. The index reloads when `index.bin`
  changes and allowlists are intersected with ids the index actually contains.
- **TV ban list by MAC** (`banned_tvs`): hides devices from discovery, scans,
  the bot, the web panel and the app. Manage it from the app (Ban button,
  Ban MAC dialog, Unban), Telegram (`/bantv`, `/unbantv`) or the web panel.
- Android app now preselects the 1080p transcode for `downgrade` TVs and casts
  registered TVs through the server.

## License

No license has been chosen yet. Add one before accepting external contributions.
