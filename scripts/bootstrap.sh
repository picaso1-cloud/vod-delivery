#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3.12}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: $PY not found (set PYTHON=/path/to/python3.12)" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "==> creating virtualenv"
  "$PY" -m venv .venv
fi

echo "==> installing dependencies"
.venv/bin/pip install -U pip wheel >/dev/null
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "==> creating .env from template"
  cp .env.example .env
fi

mkdir -p data

echo
echo "Bootstrap complete."
echo "  1) edit .env (BOT_TOKEN, XSTREAM_BASE, XSTREAM_USERNAME, XSTREAM_PASSWORD)"
echo "  2) make index    # build data/vod.db + data/index.bin"
echo "  3) make run      # start the bot (panel :8095, proxy :8091)"
echo
echo "Checks: ffmpeg must be /usr/bin/ffmpeg; python 3.12 recommended."
