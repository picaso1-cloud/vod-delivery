import asyncio
import time

_conns: dict[str, dict] = {}
_status: dict[str, dict] = {}
_bind: dict[str, str] = {}
_active: dict | None = None


def attach(player_id: str, name: str, websocket, loop, ip: str = "") -> None:
    _conns[player_id] = {
        "ws": websocket,
        "name": name,
        "ip": ip,
        "loop": loop,
        "queue": asyncio.Queue(),
        "at": time.time(),
    }


def detach(player_id: str) -> None:
    _conns.pop(player_id, None)


def online(player_id: str) -> bool:
    return player_id in _conns


def queue(player_id: str):
    conn = _conns.get(player_id)
    return conn["queue"] if conn else None


def name(player_id: str) -> str | None:
    conn = _conns.get(player_id)
    return conn["name"] if conn else None


def ip(player_id: str) -> str:
    conn = _conns.get(player_id)
    return (conn or {}).get("ip") or ""


def send(player_id: str, payload: dict) -> bool:
    conn = _conns.get(player_id)
    if conn is None:
        return False
    try:
        conn["loop"].call_soon_threadsafe(conn["queue"].put_nowait, payload)
        return True
    except RuntimeError:
        return False


def set_status(player_id: str, data: dict) -> None:
    _status[player_id] = {**data, "at": time.time()}


def get_status(player_id: str) -> dict | None:
    return _status.get(player_id)


def bind(token: str, player_id: str) -> None:
    _bind[token] = player_id


def player_for_token(token: str) -> str | None:
    return _bind.get(token)


def forget(token: str) -> None:
    _bind.pop(token, None)


def set_active(player_id: str, player_name: str, token: str, title: str, media_key: str) -> None:
    global _active
    _active = {
        "player_id": player_id,
        "name": player_name,
        "token": token,
        "title": title,
        "media_key": media_key,
        "at": time.time(),
    }


def active() -> dict | None:
    return _active


def clear_active() -> None:
    global _active
    _active = None


def online_players() -> list[dict]:
    return [{"player_id": pid, "name": conn["name"]} for pid, conn in _conns.items()]
