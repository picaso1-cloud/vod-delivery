# Setup & Regeneration Guide

This file is the single source of truth for rebuilding the project from scratch —
including the catalog index (turbovec), the embedding model, and the LLM parser.

## 0. One-command bootstrap

```bash
git clone git@github.com:picaso1-cloud/vod-delivery.git
cd vod-delivery
./scripts/bootstrap.sh          # venv + deps + .env from template
$EDITOR .env                    # fill BOT_TOKEN, XSTREAM_*
make index                      # build data/vod.db + data/index.bin
make run                        # start bot (panel :8095, proxy :8091)
```

## 1. Prerequisites

| Component | Requirement | Notes |
|---|---|---|
| OS | Linux (Ubuntu 22.04+/Debian 12+) | systemd assumed for deployment |
| Python | 3.12 | `python3.12 -m venv` |
| ffmpeg | distro build at `/usr/bin/ffmpeg` | Static builds can segfault on some provider MKVs; `src/proxy.py` and `src/subs.py` hardcode `/usr/bin/ffmpeg` |
| RAM | ~2 GB for embedding, ~1 GB runtime | embeddings run on CPU |
| Disk | ~5 GB | SQLite DB, vector index, stream cache |
| IPTV | Xtream Codes-compatible account | one concurrent connection is assumed |
| LLM | optional, OpenAI-compatible HTTP | used for NL parsing and the auditor |

## 2. Install

```bash
python3.12 -m venv .venv
.venv/bin/pip install -U pip wheel
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Dependency highlights:

- `aiogram` — Telegram bot
- `async-upnp-client` — DLNA/UPnP control
- `fastapi` + `uvicorn` — web panel and JSON API
- `turbovec` — vector index
- `sentence-transformers` — embeddings
- `httpx` — provider + LLM HTTP

## 3. Environment

`.env` (never commit it):

```dotenv
BOT_TOKEN=123456:ABC…            # from @BotFather
XSTREAM_BASE=http://provider:25461
XSTREAM_USERNAME=…
XSTREAM_PASSWORD=…
LLM_BASE_URL=http://localhost:8086/v1
LLM_MODEL=current-model
```

Settings stored in the SQLite `settings` table override `.env` (the web panel writes them):
provider credentials, `web_password`, `allowed_ids`, `app_token`, `stream_secret`, `public_base`.

`src/config.py` fails fast at import if `BOT_TOKEN` or the provider keys are missing.
Tests inject dummy values via `tests/conftest.py`, so `pytest` runs without `.env`.

## 4. Catalog index (SQLite + turbovec)

```bash
.venv/bin/python -m src.indexer
```

What it does:

1. **Fetch catalogs** from the provider API:
   - `get_vod_streams` → ~149K movies
   - `get_series` → ~25K series
   Raw JSON is disk-cached under `data/cache/` (TTL 7 days).
2. **Parse titles** (`src/parsing.py`): `|XX| Title (Year)` language prefixes,
   `||SUB-AR||` original-audio + Arabic subs, series pipe formats.
3. **Write SQLite** (`data/vod.db`): `movies`, `series`, `episodes`, plus runtime tables.
4. **Embed titles** with
   `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
   - 384 dimensions, L2-normalized, CPU (`src/embedder.py`)
   - the model (~470 MB) is downloaded on first use; cache it with `HF_HOME`
5. **Filter languages**: `INDEX_LANGS = {EN, FR, AR, original}` (~116K vectors).
6. **Build the vector index** (`data/index.bin`) with `turbovec.IdMapIndex` (`src/vector_index.py`):
   - ids are packed global ids: `movie → stream_id*2`, `series → series_id*2+1`
   - vectors must be `float32`, contiguous, and the dimension must satisfy `dim % 8 == 0`
   - `search(queries, k)` returns `(scores, ids)` — ids are `res[1]`

Re-run the indexer after changing `parsing.py` or `INDEX_LANGS`. In production it runs
weekly via `deploy/vod-indexer.service` + `vod-indexer.timer`.

## 5. LLM setup (llama.cpp)

The bot expects an OpenAI-compatible chat endpoint (default `http://localhost:8086/v1`):

```bash
llama-server -m /models/your-model.gguf \
  --host 127.0.0.1 --port 8086 \
  --ctx-size 8192 --n-gpu-layers 999
```

Notes that matter:

- **No `/v1/embeddings`**: embeddings come from `sentence-transformers` locally; the LLM
  server is only used for chat/parsing.
- A **reasoning model** is assumed: give it room. Search parsing needs `max_tokens >= 800`;
  the auditor needs `>= 4000` plus an explicit brevity instruction, or responses come back
  empty/truncated.
- Nested JSON must be extracted with a balanced-brace parser (`src/llm.py`); regex-based
  extraction truncates at the first inner `}`.
- If the LLM is unavailable, the bot falls back to rule-based parsing.

## 6. Run

```bash
.venv/bin/python -m src.bot
```

- Web panel: `http://<host>:8095` (default password `admin` — change it)
- Stream proxy: `:8091`
- The first Telegram `/start` claims ownership when the allowlist is empty.

## 7. systemd deployment

```bash
sudo cp deploy/vod-delivery.service /etc/systemd/system/
sudo cp deploy/vod-indexer.service deploy/vod-indexer.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vod-delivery.service
sudo systemctl enable --now vod-indexer.timer
```

Logs: `data/bot.log`, `data/indexer.log`. Restart with `systemctl restart vod-delivery.service`
(never kill the process manually — the panel and provider session live in the same process).

## 8. Verify

```bash
.venv/bin/python -m pytest -q            # 57 tests, no network
curl -s localhost:8095/api/status
```

Smoke checks:

- panel login works
- a search from Telegram returns posters
- `/tvs` lists your TVs
- casting a short clip works
- `GET /api/app/health` with the bearer token returns `{"ok":true}`

## 9. Disk & storage safety

Streaming writes to `data/cache/streams/`; the library lives in `data/library/`.

- **Startup sweep** — `data/cache/streams/` is emptied when the proxy starts (no stale files survive restarts).
- **Session cap** — every session is checked every 30 s; above `session_max_gb` (setting, default **8 GB**) it is killed and its file deleted. The virtual feed additionally has a hard **4 GB** cap.
- **Idle reap** — sessions with no client for 3 minutes (15 for link sessions) are killed and their files removed.
- **Free-space guard** — a new stream requires **10 GB free**; if below, stale cache is purged first, and the stream is refused with `disk full` if space is still insufficient.
- **Library budget** — `cache_limit_gb` (setting, default **50 GB**). While usage exceeds it, the download worker defers new downloads (nothing is auto-deleted).
- **Low-disk alert** — the owner receives a Telegram warning when free space drops below 15 GB.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Provider returns 403/kills streams | Send the provider user-agent (`SanSatV4Player`); the client already does this everywhere |
| Second stream fails | The provider allows **one connection**; stop the active session first |
| TV shows a black screen after casting | Check `max_served` in `/api/app/session`; if it grows, the TV is fetching. If not, the TV rejected the container/MIME |
| ffmpeg exits instantly with empty stderr | Usually a provider-side clean EOF (slot busy) or a static ffmpeg build; use `/usr/bin/ffmpeg` |
| `turbovec` ValueError on build | Embedding dimension must satisfy `dim % 8 == 0` |
| LLM returns empty content | Increase `max_tokens` (reasoning model) and keep the brevity instruction |
| Index rebuild very slow | Embeddings are CPU-bound (~400 titles/s); keep the model cached in `HF_HOME` |

## 11. Regenerate after data loss

Back up (minimum): `.env`, `data/vod.db`, `data/index.bin`, `data/library/`.

To rebuild:

```bash
# restore .env and optionally data/vod.db
make index      # rebuilds catalog + index
make run
```

The vector index is fully derived — it can always be regenerated from the provider catalogs.
