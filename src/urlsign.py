import secrets

from . import signing, store

DEFAULT_TTL = 21600


def _get(key: str, default: str = "") -> str:
    conn = store.connect()
    try:
        return store.get_setting(conn, key, default) or default
    finally:
        conn.close()


def _ensure(key: str, factory) -> str:
    value = _get(key)
    if value:
        return value
    conn = store.connect()
    try:
        value = store.get_setting(conn, key) or factory()
        store.set_setting(conn, key, value)
        return value
    finally:
        conn.close()


def stream_secret() -> str:
    return _ensure("stream_secret", lambda: secrets.token_hex(24))


def app_token() -> str:
    return _ensure("app_token", lambda: secrets.token_urlsafe(24))


def public_base() -> str:
    return _get("public_base").rstrip("/")


def signed_path(token: str, ttl: int = DEFAULT_TTL, ext: str = "") -> str:
    return signing.stream_path(stream_secret(), token, ttl, ext)


def verify(token: str, exp: str, sig: str) -> bool:
    return signing.verify(stream_secret(), token, exp, sig)


def urls_for(token: str, lan_ip: str, ttl: int = DEFAULT_TTL, ext: str = "") -> dict:
    path = signed_path(token, ttl, ext)
    data = {"path": path, "lan_url": f"http://{lan_ip}:8091{path}", "expires_in": ttl}
    base = public_base()
    if base:
        data["public_url"] = f"{base}{path}"
    return data
