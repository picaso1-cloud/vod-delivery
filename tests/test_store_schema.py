from src import store


def test_library_has_subs_columns():
    conn = store.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(library)")}
    finally:
        conn.close()
    assert "subs_path" in cols
    assert "subs_lang" in cols


def test_movies_have_probe_subs_column():
    conn = store.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(movies)")}
    finally:
        conn.close()
    assert "probe_subs" in cols


def test_settings_roundtrip():
    conn = store.connect()
    try:
        store.set_setting(conn, "_test_probe_key", "v1")
        assert store.get_setting(conn, "_test_probe_key") == "v1"
        store.set_setting(conn, "_test_probe_key", None)
        assert store.get_setting(conn, "_test_probe_key") is None
    finally:
        conn.close()
