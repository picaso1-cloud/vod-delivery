import logging
import threading
import time

from . import store
from .parsing import normalize_for_grouping

logger = logging.getLogger("vod-delivery")

_lock = threading.Lock()
_titles: set[str] = set()
_built_at = 0.0
TTL_S = 3600.0

_DENYLIST = {
    "status", "thanks", "thank you", "hello", "hi", "hey", "continue", "cancel",
    "feedback", "next episode", "next", "playing", "now", "library", "watchlist",
}


def _build() -> None:
    global _titles, _built_at
    conn = store.connect()
    try:
        titles = set()
        for (t,) in conn.execute("SELECT title FROM movies"):
            titles.add(normalize_for_grouping(t or ""))
        for (t,) in conn.execute("SELECT title FROM series"):
            titles.add(normalize_for_grouping(t or ""))
        titles.discard("")
        _titles = titles
        _built_at = time.time()
        logger.info("fast-path index built: %d normalized titles", len(titles))
    except Exception:
        logger.exception("fast-path index build failed")
    finally:
        conn.close()


def _ensure() -> None:
    with _lock:
        if not _titles or time.time() - _built_at > TTL_S:
            _build()


def refresh() -> None:
    with _lock:
        _build()


def exact_match(text: str | None) -> bool:
    if not text or len(text.strip()) < 3:
        return False
    key = normalize_for_grouping(text)
    if not key or key in _DENYLIST:
        return False
    _ensure()
    return key in _titles
