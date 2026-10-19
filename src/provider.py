import collections
import json
import time
from pathlib import Path

import httpx

from .appsettings import provider_config
from .config import PROVIDER_USER_AGENT

CACHE_DIR = Path("data/cache")
FULL_CATALOG_TTL = 7 * 86400
LIVE_TTL = 24 * 3600
EPISODE_TTL = 86400
FAIL_WINDOW_S = 900
FAIL_THRESHOLD = 3

_fail_log: collections.deque = collections.deque()
_last_error = ""


class ProviderError(RuntimeError):
    pass


def health() -> dict:
    now = time.time()
    while _fail_log and now - _fail_log[0] > FAIL_WINDOW_S:
        _fail_log.popleft()
    return {
        "down": len(_fail_log) >= FAIL_THRESHOLD,
        "recent_failures": len(_fail_log),
        "last_error": _last_error,
    }


def _record_failure(exc: Exception) -> None:
    _fail_log.append(time.time())
    global _last_error
    _last_error = f"{type(exc).__name__}: {exc}"


class Provider:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0), headers={"User-Agent": PROVIDER_USER_AGENT})
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        await self._client.aclose()

    def _cfg(self) -> dict[str, str]:
        return provider_config()

    async def _api(self, **params):
        cfg = self._cfg()
        try:
            response = await self._client.get(
                f"{cfg['xstream_base']}/player_api.php",
                params={"username": cfg["xstream_username"], "password": cfg["xstream_password"], **params},
            )
            response.raise_for_status()
        except Exception as exc:
            _record_failure(exc)
            raise
        _fail_log.clear()
        return response.json()

    async def account_info(self) -> dict:
        return await self._api()

    async def _cached_json(self, name: str, ttl: float, fetch):
        path = CACHE_DIR / f"{name}.json"
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text())
        data = await fetch()
        path.write_text(json.dumps(data, ensure_ascii=False))
        return data

    async def vod_streams(self, fresh: bool = False) -> list[dict]:
        if fresh:
            return await self._api(action="get_vod_streams")
        return await self._cached_json("vod_streams", FULL_CATALOG_TTL, lambda: self._api(action="get_vod_streams"))

    async def series(self, fresh: bool = False) -> list[dict]:
        if fresh:
            return await self._api(action="get_series")
        return await self._cached_json("series", FULL_CATALOG_TTL, lambda: self._api(action="get_series"))

    async def categories(self, kind: str) -> list[dict]:
        return await self._cached_json(f"categories_{kind}", FULL_CATALOG_TTL, lambda: self._api(action="get_categories", type=kind))

    async def series_info(self, series_id: int) -> dict:
        return await self._cached_json(
            f"series_info_{series_id}", EPISODE_TTL, lambda: self._api(action="get_series_info", series_id=series_id)
        )

    async def live_categories(self) -> list[dict]:
        return await self._cached_json("live_categories", LIVE_TTL, lambda: self._api(action="get_live_categories"))

    async def live_streams(self, category_id: int | None = None) -> list[dict]:
        if category_id is None:
            return await self._cached_json("live_streams", LIVE_TTL, lambda: self._api(action="get_live_streams"))
        return await self._cached_json(f"live_streams_cat_{category_id}", LIVE_TTL, lambda: self._api(action="get_live_streams", category_id=category_id))

    def live_url(self, stream_id: int, ext: str = "ts") -> str:
        cfg = self._cfg()
        base = cfg["xstream_base"].replace(":25461", ":88")
        return f"{base}/live/{cfg['xstream_username']}/{cfg['xstream_password']}/{stream_id}.{ext}"

    def movie_url(self, stream_id: int, ext: str) -> str:
        cfg = self._cfg()
        host = cfg["xstream_base"].replace(":25461", ":88")
        return f"{host}/movie/{cfg['xstream_username']}/{cfg['xstream_password']}/{stream_id}.{ext}"

    def episode_url(self, episode_id: int, ext: str) -> str:
        cfg = self._cfg()
        host = cfg["xstream_base"].replace(":25461", ":88")
        return f"{host}/series/{cfg['xstream_username']}/{cfg['xstream_password']}/{episode_id}.{ext}"
