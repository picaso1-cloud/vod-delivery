import re

from . import store

M3U_URL_RE = re.compile(r"(?P<base>https?://[^/]+):?\d*/get\.php\?username=(?P<user>[^&]+)&password=(?P<pass>[^&]+)", re.IGNORECASE)

PROVIDER_KEYS = ("xstream_base", "xstream_username", "xstream_password")


def provider_config(conn=None) -> dict[str, str]:
    own = conn is None
    conn = conn or store.connect()
    try:
        values = {k: store.get_setting(conn, k) for k in PROVIDER_KEYS}
    finally:
        if own:
            conn.close()
    from .config import XSTREAM_BASE, XSTREAM_PASSWORD, XSTREAM_USERNAME

    return {
        "xstream_base": values["xstream_base"] or XSTREAM_BASE,
        "xstream_username": values["xstream_username"] or XSTREAM_USERNAME,
        "xstream_password": values["xstream_password"] or XSTREAM_PASSWORD,
    }


def parse_m3u_url(url: str) -> dict[str, str] | None:
    m = M3U_URL_RE.search(url)
    if not m:
        return None
    return {
        "xstream_base": f"{m.group('base')}:25461",
        "xstream_username": m.group("user"),
        "xstream_password": m.group("pass"),
    }


def save_provider_config(conn, base: str, username: str, password: str) -> None:
    store.set_setting(conn, "xstream_base", base.rstrip("/"))
    store.set_setting(conn, "xstream_username", username.strip())
    store.set_setting(conn, "xstream_password", password.strip())


def allowed_ids(conn=None) -> set[int]:
    own = conn is None
    conn = conn or store.connect()
    try:
        raw = store.get_setting(conn, "allowed_ids") or ""
    finally:
        if own:
            conn.close()
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def add_allowed_id(conn, user_id: int) -> None:
    ids = allowed_ids(conn)
    ids.add(user_id)
    store.set_setting(conn, "allowed_ids", ",".join(str(i) for i in sorted(ids)))


def is_allowed(user_id: int, conn=None) -> bool:
    own = conn is None
    conn = conn or store.connect()
    try:
        ids = allowed_ids(conn)
        if ids:
            return user_id in ids
        store.set_setting(conn, "allowed_ids", str(user_id))
        return True
    finally:
        if own:
            conn.close()
