import asyncio
import secrets
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import store
from . import library
from .library import owner_chat_id
from .appsettings import parse_m3u_url, provider_config, save_provider_config
from .version import __version__ as APP_VERSION
from .scanner import scan_ip

WEB_PORT = 8095
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "web_templates"))

app = FastAPI(title="VOD-Delivery", docs_url=None, redoc_url=None)
_sessions: set[str] = set()


def _db():
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


def _check_password(conn, password: str) -> bool:
    expected = store.get_setting(conn, "web_password") or "admin"
    return password == expected


def _auth(request: Request, conn=Depends(_db)):
    token = request.cookies.get("vod_session", "")
    if token not in _sessions:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return conn


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
def login(request: Request, password: str = Form(""), conn=Depends(_db)):
    if not _check_password(conn, password):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": "Wrong password"}, status_code=303)
    token = secrets.token_hex(16)
    _sessions.add(token)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("vod_session", token, httponly=True)
    return response


@app.get("/logout")
def logout():
    _sessions.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(_auth)):
    from .bot import active_play

    active = {}
    if active_play is not None:
        active = {"tv": active_play.tv_name, "media_key": active_play.media_key}
    account = {}
    try:
        from .provider import Provider

        info = asyncio.run(Provider().account_info())
        account = info.get("user_info", {})
    except Exception as exc:
        account = {"status": f"provider error: {exc}"}
    plays = store.recent_plays(conn, 10)
    tvs = store.list_tvs(conn)
    from . import library as libmod

    usage_gb = libmod.cache_usage_bytes() / 1024**3
    limit_gb = libmod.cache_limit_bytes() / 1024**3
    cache_over = usage_gb > limit_gb
    continue_items = conn.execute(
        "SELECT * FROM watch_progress WHERE position_s > 30 AND (duration_s = 0 OR position_s < duration_s * 0.95) "
        "ORDER BY updated_at DESC LIMIT 8"
    ).fetchall()
    from datetime import datetime, timezone

    exp_date = ""
    if account.get("exp_date"):
        try:
            exp_date = datetime.fromtimestamp(int(account["exp_date"]), tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            exp_date = str(account["exp_date"])
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "usage_gb": usage_gb, "limit_gb": limit_gb, "cache_over": cache_over,
            "active": active,
            "account": account,
            "exp_date": exp_date,
            "plays": plays,
            "tv_count": len(tvs),
            "continue_items": continue_items,
            "progress_title": lambda key: store.progress_title(conn, key),
        },
    )


def _title_for(conn, media_key: str) -> str:
    return store.progress_title(conn, media_key)


@app.get("/tvs", response_class=HTMLResponse)
def tvs_page(request: Request, conn=Depends(_auth)):
    return TEMPLATES.TemplateResponse(request, "tvs.html", {"tvs": store.list_tvs(conn), "saved": request.query_params.get("saved")})


@app.post("/tvs/{udn}/rename")
def tv_rename(udn: str, name: str = Form(""), conn=Depends(_auth)):
    if name.strip():
        store.rename_tv(conn, udn, name.strip())
        conn.execute("UPDATE tvs SET source='renamed' WHERE udn=?", (udn,))
        conn.commit()
    return RedirectResponse("/tvs?saved=1", status_code=303)


@app.post("/tvs/{udn}/flags")
def tv_flags(udn: str, wol: str = Form(""), prefer_proxy: str = Form(""), downgrade: str = Form(""), ignored: str = Form(""), mac: str = Form(""), conn=Depends(_auth)):
    store.set_tv_flags(
        conn,
        udn,
        wol_enabled=1 if wol == "on" else 0,
        prefer_proxy=1 if prefer_proxy == "on" else 0,
        downgrade=1 if downgrade == "on" else 0,
        mac=mac.strip() or None,
    )
    conn.execute("UPDATE tvs SET ignored=? WHERE udn=?", (1 if ignored == "on" else 0, udn))
    conn.commit()
    return RedirectResponse("/tvs?saved=1", status_code=303)


@app.post("/tvs/scan")
async def tv_scan(ip: str = Form(""), conn=Depends(_auth)):
    from .scanner import register, valid_ip

    if not valid_ip(ip):
        return RedirectResponse("/tvs?error=Invalid+IP", status_code=303)
    result = await scan_ip(ip)
    if result.get("validated"):
        udn = register(result)
        kind = result["kind"]
        return RedirectResponse(
            f"/tvs?saved=1&scan={kind}+TV+identified%3A+{result.get('name','?')}+%28{ip}%29+and+registered",
            status_code=303,
        )
    notes = " · ".join(result.get("notes", [])) or "nothing recognizable"
    return RedirectResponse(f"/tvs?error=Scan+failed%3A+{notes}", status_code=303)


@app.post("/tvs/{udn}/delete")
def tv_delete(udn: str, conn=Depends(_auth)):
    store.delete_tv(conn, udn)
    return RedirectResponse("/tvs", status_code=303)


@app.post("/tvs/add")
def tv_add(ip: str = Form(""), mac: str = Form(""), name: str = Form(""), kind: str = Form("dlna"), conn=Depends(_auth)):
    if mac.strip():
        identity = f"{kind}::{mac.strip().lower()}"
        conn.execute(
            "INSERT INTO tvs(udn, name, ip, mac, kind, source) VALUES(?,?,?,?,?, 'manual') "
            "ON CONFLICT(udn) DO UPDATE SET name=excluded.name, ip=excluded.ip, mac=excluded.mac, kind=excluded.kind",
            (identity, name.strip() or f"TV {ip.strip() or mac.strip()}", ip.strip() or None, mac.strip().lower(), kind),
        )
        conn.commit()
    elif ip.strip():
        udn = f"manual::{ip.strip()}"
        conn.execute(
            "INSERT INTO tvs(udn, name, ip, mac, kind, source) VALUES(?,?,?,?,?, 'manual') "
            "ON CONFLICT(udn) DO UPDATE SET name=excluded.name, ip=excluded.ip, mac=excluded.mac, kind=excluded.kind",
            (udn, name.strip() or f"TV {ip.strip()}", ip.strip(), mac.strip() or None, kind),
        )
        conn.commit()
    return RedirectResponse("/tvs?saved=1", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, conn=Depends(_auth)):
    return TEMPLATES.TemplateResponse(request, "history.html", {"plays": store.recent_plays(conn, 100)})


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, conn=Depends(_auth)):
    items = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
    return TEMPLATES.TemplateResponse(request, "watchlist.html", {"items": items})


@app.post("/stop")
def stop_playback(conn=Depends(_auth)):
    from .bot import _stop_active

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_stop_active("web panel"))
        loop.close()
    except Exception:
        pass
    return RedirectResponse("/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn=Depends(_auth)):
    cfg = provider_config(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": store.all_settings(conn),
            "cfg": cfg,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/settings/provider")
def settings_provider(
    xstream_base: str = Form(""),
    xstream_username: str = Form(""),
    xstream_password: str = Form(""),
    conn=Depends(_auth),
):
    if xstream_base.strip() and xstream_username.strip() and xstream_password.strip():
        save_provider_config(conn, xstream_base, xstream_username, xstream_password)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/m3u")
def settings_m3u(m3u_url: str = Form(""), conn=Depends(_auth)):
    parsed = parse_m3u_url(m3u_url)
    if not parsed:
        return RedirectResponse("/settings?error=Could+not+parse+that+M3U+URL", status_code=303)
    save_provider_config(conn, parsed["xstream_base"], parsed["xstream_username"], parsed["xstream_password"])
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/access")
def settings_access(allowed_ids: str = Form(""), web_password: str = Form(""), conn=Depends(_auth)):
    ids = ",".join(x.strip() for x in allowed_ids.split(",") if x.strip().isdigit())
    store.set_setting(conn, "allowed_ids", ids)
    if web_password.strip():
        store.set_setting(conn, "web_password", web_password.strip())
    return RedirectResponse("/settings?saved=1", status_code=303)


def start_in_thread() -> None:
    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=WEB_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="vod-web")
    thread.start()


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, conn=Depends(_db)):
    from . import library as libmod

    usage = libmod.cache_usage_bytes()
    limit = libmod.cache_limit_bytes()
    items = store.list_library(conn)
    queue = store.list_queue(conn)
    return TEMPLATES.TemplateResponse(request, "library.html", {
        "items": items,
        "queue": queue,
        "usage_gb": usage / 1024**3,
        "limit_gb": limit / 1024**3,
        "over": usage > limit,
        "saved": request.query_params.get("saved"),
        "error": request.query_params.get("error"),
    })


@app.post("/library/scan-inbox")
def library_scan_inbox():
    from . import library as libmod

    result = libmod.scan_inbox()
    msg = f"Imported {len(result['imported'])}, skipped {len(result['skipped'])}"
    return RedirectResponse(f"/library?saved={msg.replace(' ', '+')}", status_code=303)


def _lib_action(request: Request, media_key: str, action: str):
    from . import library as libmod

    conn = store.connect()
    try:
        item = store.get_library_item(conn, media_key)
    finally:
        conn.close()
    if item is None:
        return RedirectResponse("/library?error=Not+found", status_code=303)
    if action == "delete":
        libmod.delete_media(media_key)
        return RedirectResponse("/library?saved=Deleted+(marked+online-only)", status_code=303)
    if action == "archive":
        ok, msg = libmod.archive_media(media_key)
        if ok:
            return RedirectResponse("/library?saved=Archived", status_code=303)
        return RedirectResponse(f"/library?error={msg.replace(' ', '+')}", status_code=303)
    if action == "redl":
        from .provider import Provider

        conn = store.connect()
        try:
            provider = Provider()
            if item["kind"] == "movie":
                row = conn.execute("SELECT ext FROM movies WHERE stream_id=?", (item["ref_id"],)).fetchone()
                url = provider.movie_url(item["ref_id"], row["ext"]) if row else ""
            else:
                row = conn.execute("SELECT ext FROM episodes WHERE episode_id=?", (item["ref_id"],)).fetchone()
                url = provider.episode_url(item["ref_id"], row["ext"]) if row else ""
            ext = row["ext"] if row else "mkv"
            store.set_library_status(conn, media_key, "online_only")
            store.enqueue_download(conn, media_key, item["kind"], item["ref_id"], item["title"], url, ext, source="manual")
        finally:
            conn.close()
        return RedirectResponse("/library?saved=Queued+for+download", status_code=303)
    return RedirectResponse("/library", status_code=303)


@app.post("/library/{media_key}/delete")
def library_delete(media_key: str, conn=Depends(_db)):
    return _lib_action(request=None, media_key=media_key, action="delete")


@app.post("/library/{media_key}/archive")
def library_archive(media_key: str, conn=Depends(_db)):
    return _lib_action(request=None, media_key=media_key, action="archive")


@app.post("/library/{media_key}/redl")
def library_redl(media_key: str, conn=Depends(_db)):
    return _lib_action(request=None, media_key=media_key, action="redl")


@app.post("/library/queue/{media_key}/cancel")
def library_queue_cancel(media_key: str, conn=Depends(_db)):
    store.remove_queue_item(conn, media_key)
    return RedirectResponse("/library?saved=Removed+from+queue", status_code=303)


BRIDGE: dict = {}


def bind(**fns) -> None:
    BRIDGE.update(fns)


def call_bridge(name: str, *args, timeout: float = 180.0):
    import asyncio as _asyncio

    loop = BRIDGE.get("loop")
    fn = BRIDGE.get(name)
    if loop is None or fn is None:
        raise HTTPException(status_code=503, detail="bot bridge not ready")
    fut = _asyncio.run_coroutine_threadsafe(fn(*args), loop)
    return fut.result(timeout=timeout)


def _app_auth(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):
        return
    from . import urlsign

    expected = urlsign.app_token()
    header = request.headers.get("authorization", "")
    if expected and secrets.compare_digest(header, f"Bearer {expected}"):
        return
    raise HTTPException(status_code=401, detail="app token required")


def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="localhost only")


@app.get("/api/search")
async def api_search(request: Request, q: str = "", type: str = "movie"):
    _local_only(request)
    return call_bridge("search", q, type)


@app.get("/api/episodes")
async def api_episodes(request: Request, series_id: int):
    _local_only(request)
    return call_bridge("episodes", series_id)


@app.get("/api/tvs")
async def api_tvs(request: Request):
    _local_only(request)
    return call_bridge("tvs")


@app.post("/api/play")
async def api_play(request: Request, media_key: str = "", tv: str = "", start_position: float = 0.0):
    _local_only(request)
    if not media_key:
        raise HTTPException(status_code=422, detail="media_key required")
    return call_bridge("play", media_key, tv, float(start_position), timeout=900.0)


@app.post("/api/live")
async def api_live(request: Request, channel_id: int = 0, tv: str = ""):
    _local_only(request)
    if not channel_id:
        raise HTTPException(status_code=422, detail="channel_id required")
    return call_bridge("live_play", int(channel_id), tv)


@app.post("/api/control")
async def api_control(request: Request, action: str = "", value: float = 0.0):
    _local_only(request)
    return call_bridge("control", action, float(value))


@app.post("/api/test/virtual")
async def api_test_virtual(request: Request, tv: str = "", file_path: str = "", start_position: float = 0.0, seek_to: float = -1.0, transcode: bool = True, loop: bool = False):
    _local_only(request)
    return call_bridge("test_virtual", tv, file_path, float(start_position), float(seek_to), bool(transcode), bool(loop))


def _wake_mac(mac: str) -> bool:
    import socket
    import time as _t

    clean = (mac or "").replace(":", "").replace("-", "").strip()
    if len(clean) != 12:
        return False
    payload = b"\xff" * 6 + bytes.fromhex(clean) * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(3):
            for dst in ("255.255.255.255", "192.168.1.255"):
                for port in (9, 7):
                    try:
                        sock.sendto(payload, (dst, port))
                    except OSError:
                        pass
            _t.sleep(0.15)
    finally:
        sock.close()
    return True


def _tv_by_name(conn, name: str):
    return conn.execute("SELECT * FROM tvs WHERE ignored=0 AND name LIKE ?", (f"%{name}%",)).fetchone()


@app.get("/api/app/whoami", dependencies=[Depends(_app_auth)])
async def app_whoami():
    return {"ok": True}


@app.get("/api/app/config", dependencies=[Depends(_app_auth)])
async def app_config():
    return call_bridge("app_config")


@app.get("/api/app/search", dependencies=[Depends(_app_auth)])
async def app_search(q: str = "", type: str = "movie"):
    return call_bridge("search", q, type)


@app.get("/api/app/episodes", dependencies=[Depends(_app_auth)])
async def app_episodes(series_id: int):
    return call_bridge("episodes", series_id)


@app.get("/api/app/tvs", dependencies=[Depends(_app_auth)])
async def app_tvs():
    return call_bridge("tvs")


@app.get("/api/app/tvs/detail", dependencies=[Depends(_app_auth)])
async def app_tv_detail(name: str = ""):
    if not name:
        raise HTTPException(status_code=422, detail="name required")
    return call_bridge("tv_detail", name)


@app.post("/api/app/tvs/probe", dependencies=[Depends(_app_auth)])
async def app_tv_probe(name: str = ""):
    if not name:
        raise HTTPException(status_code=422, detail="name required")
    return call_bridge("tv_probe", name, timeout=180.0)


@app.post("/api/app/stream", dependencies=[Depends(_app_auth)])
async def app_stream(media_key: str = "", start_position: float = 0.0, mode: str = "cache",
                     quality: str = "copy", prepare: int = 0, format: str = ""):
    if not media_key:
        raise HTTPException(status_code=422, detail="media_key required")
    return call_bridge("app_stream", media_key, float(start_position), mode, quality,
                       int(prepare), format, timeout=300.0)


@app.get("/api/app/session", dependencies=[Depends(_app_auth)])
async def app_session(token: str = ""):
    if not token:
        raise HTTPException(status_code=422, detail="token required")
    return call_bridge("app_session", token)


@app.post("/api/app/control", dependencies=[Depends(_app_auth)])
async def app_control(token: str = "", action: str = "", value: float = 0.0):
    if not token:
        raise HTTPException(status_code=422, detail="token required")
    return call_bridge("app_control", token, action, float(value))


@app.get("/api/app/status", dependencies=[Depends(_app_auth)])
async def app_status():
    return call_bridge("status")


@app.get("/api/app/health", dependencies=[Depends(_app_auth)])
async def app_health():
    conn = store.connect()
    try:
        tvs = conn.execute("SELECT COUNT(*) FROM tvs WHERE ignored=0").fetchone()[0]
        lib = conn.execute("SELECT COUNT(*) FROM library").fetchone()[0]
        queue = conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "version": APP_VERSION, "tvs": tvs, "library": lib, "queue": queue}


@app.get("/api/app/live", dependencies=[Depends(_app_auth)])
async def app_live(q: str = ""):
    return call_bridge("search", q, "live")


@app.post("/api/app/play", dependencies=[Depends(_app_auth)])
async def app_play(media_key: str = "", tv: str = "", start_position: float = 0.0):
    if not media_key:
        raise HTTPException(status_code=422, detail="media_key required")
    return call_bridge("play", media_key, tv, float(start_position), timeout=900.0)


@app.post("/api/app/live/play", dependencies=[Depends(_app_auth)])
async def app_live_play(channel_id: int = 0, tv: str = ""):
    if not channel_id:
        raise HTTPException(status_code=422, detail="channel_id required")
    return call_bridge("live_play", channel_id, tv, timeout=300.0)


@app.get("/tv/{token}", response_class=HTMLResponse)
def tv_receiver(token: str, exp: str = "", sig: str = "", ext: str = "ts"):
    import html as _html
    import re as _re

    from . import proxy, urlsign

    if not _re.fullmatch(r"[a-f0-9]{12}", token) or ext not in ("ts", "mkv", "mp4"):
        raise HTTPException(status_code=404, detail="not found")
    base = urlsign.public_base() or f"http://{proxy.lan_ip_for('192.168.1.1')}:8091"
    src = _html.escape(f"{base}/s/{token}.{ext}?exp={exp}&sig={sig}")
    return HTMLResponse(
        "<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>VOD</title><style>html,body{margin:0;background:#000;height:100%}"
        "video{width:100%;height:100vh;background:#000}</style></head><body>"
        f"<video controls autoplay playsinline src='{src}'></video></body></html>"
    )


@app.post("/api/app/tvs/scan", dependencies=[Depends(_app_auth)])
async def app_tv_scan(ip: str = ""):
    from .scanner import register, scan_ip, valid_ip

    if not valid_ip(ip):
        raise HTTPException(status_code=422, detail="invalid IP")
    result = await scan_ip(ip)
    if result.get("validated"):
        register(result)
        return {"ok": True, "kind": result.get("kind"), "name": result.get("name"), "ip": ip}
    return {"ok": False, "notes": result.get("notes", [])}


@app.post("/api/app/tvs/wake", dependencies=[Depends(_app_auth)])
async def app_tv_wake(name: str = ""):
    conn = store.connect()
    try:
        row = _tv_by_name(conn, name)
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "TV not found"}
    if not row["mac"]:
        return {"ok": False, "error": "no MAC on record"}
    return {"ok": _wake_mac(row["mac"]), "mac": row["mac"]}


@app.post("/api/app/tvs/flags", dependencies=[Depends(_app_auth)])
async def app_tv_flags(name: str = "", prefer_proxy: int | None = None, downgrade: int | None = None,
                       v2_seek: int | None = None, virtual_channel: int | None = None):
    conn = store.connect()
    try:
        row = _tv_by_name(conn, name)
        if row is None:
            return {"ok": False, "error": "TV not found"}
        sets = []
        vals = []
        for col, val in (("prefer_proxy", prefer_proxy), ("downgrade", downgrade),
                         ("v2_seek", v2_seek), ("virtual_channel", virtual_channel)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(1 if val else 0)
        if sets:
            conn.execute(f"UPDATE tvs SET {', '.join(sets)} WHERE udn=?", (*vals, row["udn"]))
            conn.commit()
        updated = conn.execute("SELECT * FROM tvs WHERE udn=?", (row["udn"],)).fetchone()
        return {"ok": True, "tv": dict(updated)}
    finally:
        conn.close()


@app.post("/api/app/tvs/rename", dependencies=[Depends(_app_auth)])
async def app_tv_rename(name: str = "", new_name: str = ""):
    if not new_name.strip():
        raise HTTPException(status_code=422, detail="new_name required")
    conn = store.connect()
    try:
        row = _tv_by_name(conn, name)
        if row is None:
            return {"ok": False, "error": "TV not found"}
        store.rename_tv(conn, row["udn"], new_name.strip())
        return {"ok": True, "name": new_name.strip()}
    finally:
        conn.close()


@app.get("/api/app/library", dependencies=[Depends(_app_auth)])
async def app_library():
    conn = store.connect()
    try:
        items = []
        for r in store.list_library(conn):
            d = dict(r)
            d.pop("url", None)
            items.append(d)
        usage = store.library_size_bytes(conn)
    finally:
        conn.close()
    return {"ok": True, "items": items, "usage_bytes": usage}


@app.post("/api/app/library/delete", dependencies=[Depends(_app_auth)])
async def app_library_delete(media_key: str = ""):
    from . import library as libmod

    if not media_key:
        raise HTTPException(status_code=422, detail="media_key required")
    ok = libmod.delete_media(media_key)
    return {"ok": bool(ok)}


@app.post("/api/app/library/archive", dependencies=[Depends(_app_auth)])
async def app_library_archive(media_key: str = ""):
    from . import library as libmod

    ok, msg = libmod.archive_media(media_key)
    return {"ok": bool(ok), "detail": msg}


@app.post("/api/app/library/redownload", dependencies=[Depends(_app_auth)])
async def app_library_redownload(media_key: str = ""):
    from .provider import Provider

    conn = store.connect()
    try:
        item = store.get_library_item(conn, media_key)
        if item is None:
            return {"ok": False, "error": "not found"}
        provider = Provider()
        if item["kind"] == "movie":
            row = conn.execute("SELECT ext FROM movies WHERE stream_id=?", (item["ref_id"],)).fetchone()
            url = provider.movie_url(item["ref_id"], row["ext"]) if row else ""
        else:
            row = conn.execute("SELECT ext FROM episodes WHERE episode_id=?", (item["ref_id"],)).fetchone()
            url = provider.episode_url(item["ref_id"], row["ext"]) if row else ""
        ext = row["ext"] if row else "mkv"
        store.set_library_status(conn, media_key, "online_only")
        store.enqueue_download(conn, media_key, item["kind"], item["ref_id"], item["title"], url, ext, source="manual")
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/app/queue", dependencies=[Depends(_app_auth)])
async def app_queue():
    conn = store.connect()
    try:
        items = []
        for r in store.list_queue(conn):
            d = dict(r)
            d.pop("url", None)
            items.append(d)
        return {"ok": True, "items": items}
    finally:
        conn.close()


@app.post("/api/app/queue/cancel", dependencies=[Depends(_app_auth)])
async def app_queue_cancel(media_key: str = ""):
    conn = store.connect()
    try:
        store.remove_queue_item(conn, media_key)
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/app/watchlist", dependencies=[Depends(_app_auth)])
async def app_watchlist():
    conn = store.connect()
    try:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
        return {"ok": True, "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/app/watchlist/add", dependencies=[Depends(_app_auth)])
async def app_watchlist_add(media_key: str = ""):
    kind, _, ref = media_key.partition(":")
    if kind not in ("movie", "series") or not ref.isdigit():
        raise HTTPException(status_code=422, detail="media_key must be movie:<id> or series:<id>")
    ref_id = int(ref)
    conn = store.connect()
    try:
        if kind == "movie":
            row = conn.execute("SELECT title, year FROM movies WHERE stream_id=?", (ref_id,)).fetchone()
            title = row["title"] if row else media_key
            year = row["year"] if row else None
            poster = conn.execute("SELECT poster FROM movies WHERE stream_id=?", (ref_id,)).fetchone()
            poster = poster["poster"] if poster else None
        else:
            row = conn.execute("SELECT title FROM series WHERE series_id=?", (ref_id,)).fetchone()
            title = row["title"] if row else media_key
            year = None
            poster = conn.execute("SELECT poster FROM series WHERE series_id=?", (ref_id,)).fetchone()
            poster = poster["poster"] if poster else None
        store.add_watchlist(conn, library.owner_chat_id() or 0, kind, ref_id, title, year, poster)
        return {"ok": True, "title": title}
    finally:
        conn.close()


@app.post("/api/app/watchlist/remove", dependencies=[Depends(_app_auth)])
async def app_watchlist_remove(media_key: str = ""):
    kind, _, ref = media_key.partition(":")
    if kind not in ("movie", "series") or not ref.isdigit():
        raise HTTPException(status_code=422, detail="media_key must be movie:<id> or series:<id>")
    conn = store.connect()
    try:
        store.remove_watchlist(conn, library.owner_chat_id() or 0, kind, int(ref))
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/app/history", dependencies=[Depends(_app_auth)])
async def app_history(limit: int = 50):
    conn = store.connect()
    try:
        return {"ok": True, "items": [dict(r) for r in store.recent_plays(conn, max(1, min(limit, 200)))]}
    finally:
        conn.close()


@app.get("/api/app/audit", dependencies=[Depends(_app_auth)])
async def app_audit(limit: int = 20):
    import json as _json
    from pathlib import Path as _Path

    path = _Path(__file__).resolve().parent.parent / "data" / "audit" / "findings.jsonl"
    if not path.exists():
        return {"ok": True, "items": []}
    lines = path.read_text(errors="replace").splitlines()[-max(1, min(limit, 100)):]
    items = []
    for line in lines:
        try:
            items.append(_json.loads(line))
        except ValueError:
            continue
    return {"ok": True, "items": items}


@app.get("/api/status")
async def api_status(request: Request):
    _local_only(request)
    return call_bridge("status")
