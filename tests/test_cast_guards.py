import asyncio
import time

import pytest

from src import bot, players, proxy


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, codes):
        self.codes = list(codes)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        code = self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]
        return _FakeStream(_FakeResponse(code))


def _session(token="tok", **extra):
    session = proxy.ProxySession(token=token, provider_url="http://provider/x.mkv", title="X")
    session.ready.set()
    for key, value in extra.items():
        setattr(session, key, value)
    proxy._sessions[token] = session
    return session


def _cleanup():
    proxy._sessions.clear()
    bot._PLAY_AT.clear()
    bot._PLAY_INFLIGHT.clear()
    players.clear_active()


def test_duplicate_play_is_ignored_while_the_session_is_running():
    _session("tok", max_served=1024, clients=1)
    players.set_active("pid", "TV", "tok", "Title", "movie:1")
    bot._PLAY_AT["pid"] = ("movie:1", time.time())
    assert bot._recent_duplicate_play("pid", "movie:1") is True
    assert bot._recent_duplicate_play("pid", "movie:2") is False
    _cleanup()


def test_duplicate_play_is_allowed_for_another_player():
    _session("tok", max_served=1024, clients=1)
    players.set_active("pid", "TV", "tok", "Title", "movie:1")
    bot._PLAY_AT["pid"] = ("movie:1", time.time())
    assert bot._recent_duplicate_play("other", "movie:1") is False
    _cleanup()


def test_duplicate_play_is_allowed_after_the_window():
    _session("tok", max_served=1024, clients=1)
    players.set_active("pid", "TV", "tok", "Title", "movie:1")
    bot._PLAY_AT["pid"] = ("movie:1", time.time() - bot.PLAY_DEBOUNCE_S - 1)
    assert bot._recent_duplicate_play("pid", "movie:1") is False
    _cleanup()


def test_duplicate_play_is_allowed_when_the_session_died():
    session = _session("tok", max_served=1024, clients=1)
    session.done = True
    players.set_active("pid", "TV", "tok", "Title", "movie:1")
    bot._PLAY_AT["pid"] = ("movie:1", time.time())
    assert bot._recent_duplicate_play("pid", "movie:1") is False
    _cleanup()


def test_duplicate_play_is_ignored_while_the_first_request_is_still_starting():
    bot._PLAY_INFLIGHT["pid"] = ("movie:1", time.time())
    assert bot._recent_duplicate_play("pid", "movie:1") is True
    assert bot._recent_duplicate_play("pid", "movie:2") is False
    _cleanup()


def test_stop_tv_app_preempts_a_previous_player(monkeypatch):
    sent = []
    stopped = []
    monkeypatch.setattr(players, "send", lambda pid, payload: sent.append((pid, payload)) or True)
    monkeypatch.setattr(proxy, "stop_session", lambda token: stopped.append(token) or asyncio.sleep(0))

    async def run():
        _session("tok")
        players.set_active("old", "Old TV", "tok", "Title", "movie:1")
        note = await bot._stop_tv_app("new cast")
        return note

    note = asyncio.run(run())
    assert "Old TV" in note
    assert sent == [("old", {"type": "control", "action": "stop"})]
    assert stopped == ["tok"]
    assert players.active() is None
    _cleanup()


def test_stop_tv_app_keeps_the_target_player(monkeypatch):
    sent = []
    monkeypatch.setattr(players, "send", lambda pid, payload: sent.append(pid) or True)

    async def run():
        _session("tok")
        players.set_active("keep", "TV", "tok", "Title", "movie:1")
        return await bot._stop_tv_app("switch", except_player_id="keep")

    assert asyncio.run(run()) == ""
    assert sent == []
    assert players.active()["player_id"] == "keep"
    _cleanup()


def test_await_provider_slot_returns_once_the_provider_accepts(monkeypatch):
    client = _FakeClient([403, 206])
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client)
    assert asyncio.run(proxy.await_provider_slot("http://provider/x.mkv", budget=2)) is True
    assert client.codes == [206]


def test_await_provider_slot_refuses_when_the_slot_never_frees(monkeypatch):
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: _FakeClient([403]))
    with pytest.raises(proxy.ProviderBusy):
        asyncio.run(proxy.await_provider_slot("http://provider/x.mkv", budget=0.1))


def test_kill_cancels_session_tasks():
    async def run():
        session = _session("tok")
        sleeper = asyncio.create_task(asyncio.sleep(60))
        proxy._track(session, sleeper)
        await proxy._kill(session)
        await asyncio.sleep(0)
        return session, sleeper

    session, sleeper = asyncio.run(run())
    assert session.done is True
    assert sleeper.cancelled() is True
    _cleanup()
