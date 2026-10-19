#!/usr/bin/env python3
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUPS = ROOT / "data" / "backups"
KEEP = 7


def rotate() -> None:
    stamps = sorted((BACKUPS / n for n in os.listdir(BACKUPS) if n.startswith("backup-")), key=lambda p: p.stat().st_mtime)
    for old in stamps[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def main() -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS / f"backup-{stamp}"
    dest.mkdir()
    src_db = sqlite3.connect(ROOT / "data" / "vod.db")
    out = sqlite3.connect(dest / "vod.db")
    src_db.backup(out)
    out.close()
    src_db.close()
    env_src = ROOT / ".env"
    if env_src.exists():
        shutil.copy2(env_src, dest / ".env")
    audit = ROOT / "data" / "audit"
    if audit.exists():
        shutil.copytree(audit, dest / "audit", dirs_exist_ok=True)
    rotate()
    print(f"backup ok: {dest}")


if __name__ == "__main__":
    sys.exit(main())
