import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "000000000:TEST-TOKEN")
os.environ.setdefault("XSTREAM_BASE", "http://127.0.0.1:25461")
os.environ.setdefault("XSTREAM_USERNAME", "test")
os.environ.setdefault("XSTREAM_PASSWORD", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
