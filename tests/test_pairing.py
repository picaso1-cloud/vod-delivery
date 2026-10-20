import pathlib

from src import store


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", pathlib.Path(tmp_path) / "vod.db")
    return store.connect()


def test_pair_request_lifecycle(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    try:
        req = store.create_pair_request(conn, "Pixel 8", "android 14", "inst-1", "192.168.1.9")
        assert len(req["id"]) == 24
        assert len(req["code"]) == 8
        assert store.pair_status(conn, req["id"], "WRONG") is None
        assert store.pair_status(conn, req["id"], req["code"]) == {"status": "pending"}
        token = store.approve_pair_request(conn, req["id"])
        assert token
        assert store.valid_app_token(conn, token)
        assert store.pair_status(conn, req["id"], req["code"]) == {"status": "approved", "token": token}
        assert store.pair_status(conn, req["id"], req["code"]) == {"status": "claimed"}
        assert store.approve_pair_request(conn, req["id"]) is None
        store.revoke_app_token(conn, token)
        assert not store.valid_app_token(conn, token)
    finally:
        conn.close()


def test_pair_denied(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    try:
        req = store.create_pair_request(conn, "Tablet", "android")
        assert store.deny_pair_request(conn, req["id"]) is True
        assert store.pair_status(conn, req["id"], req["code"]) == {"status": "denied"}
        assert store.approve_pair_request(conn, req["id"]) is None
        assert store.deny_pair_request(conn, req["id"]) is False
    finally:
        conn.close()


def test_pair_expiry(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    try:
        req = store.create_pair_request(conn, "Old phone")
        conn.execute(
            "UPDATE pair_requests SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", req["id"]),
        )
        conn.commit()
        assert store.pair_status(conn, req["id"], req["code"]) == {"status": "expired"}
        assert store.approve_pair_request(conn, req["id"]) is None
    finally:
        conn.close()


def test_pair_pending_count(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    try:
        assert store.count_pending_pairs(conn) == 0
        a = store.create_pair_request(conn, "A")
        store.create_pair_request(conn, "B")
        assert store.count_pending_pairs(conn) == 2
        store.deny_pair_request(conn, a["id"])
        assert store.count_pending_pairs(conn) == 1
    finally:
        conn.close()


def test_issue_tokens_unique(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    try:
        a = store.issue_app_token(conn, "phone")
        b = store.issue_app_token(conn, "tablet")
        assert a != b
        labels = {r["label"] for r in store.list_app_tokens(conn)}
        assert labels == {"phone", "tablet"}
        store.revoke_app_token(conn, a)
        assert {r["label"] for r in store.list_app_tokens(conn)} == {"tablet"}
    finally:
        conn.close()
