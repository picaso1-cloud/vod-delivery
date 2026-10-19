"""MCP stdio server exposing the VOD-delivery bot as tools for LLM agents.

Tools map to the localhost-only /api/* endpoints of the web panel (:8095),
which executes inside the bot process (search, play, control, status).
"""

import httpx
from mcp.server.mcpserver import MCPServer

BASE = "http://127.0.0.1:8095"
TIMEOUT = 300.0

mcp = MCPServer("vod-delivery")


def _call(method: str, path: str, params: dict | None = None) -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.request(method, BASE + path, params=params)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def search(query: str, type: str = "movie") -> list:
    """Search the IPTV catalog. type: movie | series | live.
    Returns results with media_key values used by play(). For series use
    episodes(series_id) to list episodes; each episode has its own media_key."""
    return _call("GET", "/api/search", {"q": query, "type": type})


@mcp.tool()
def episodes(series_id: int) -> dict:
    """List all episodes of a series. series_id comes from search(type='series')
    variants[].media_key ('series:<id>'). Returns {episodes: {season: [ep...]}}."""
    return _call("GET", "/api/episodes", {"series_id": series_id})


@mcp.tool()
def list_tvs() -> list:
    """List registered TVs (name, ip, kind, prefer_proxy, downgrade)."""
    return _call("GET", "/api/tvs")


@mcp.tool()
def play(media_key: str, tv: str = "", start_position: float = 0) -> dict:
    """Start playing on a TV. media_key from search()/episodes()
    ('movie:<id>', 'episode:<id>:<series_id>', 'series:<id>' won't play directly).
    tv: (partial) TV name; empty = first registered TV. start_position: seconds
    into the content (e.g. 1800 = minute 30). Waits until the TV reports a state."""
    return _call("POST", "/api/play", {"media_key": media_key, "tv": tv, "start_position": start_position})


@mcp.tool()
def play_live(channel_id: int, tv: str = "") -> dict:
    """Start a live TV channel on a TV. channel_id from search(type='live')."""
    return _call("POST", "/api/live", {"channel_id": channel_id, "tv": tv})


@mcp.tool()
def control(action: str, value: float = 0) -> dict:
    """Control playback on the active TV. action: pause | resume | seek | stop.
    seek: value is +/- seconds relative to now (server-side on seek-less TVs)."""
    return _call("POST", "/api/control", {"action": action, "value": value})


@mcp.tool()
def status() -> dict:
    """What is playing right now: TV, paused state, position/duration."""
    return _call("GET", "/api/status")


if __name__ == "__main__":
    mcp.run(transport='stdio')
