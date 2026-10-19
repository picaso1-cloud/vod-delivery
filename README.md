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

## License

No license has been chosen yet. Add one before accepting external contributions.
