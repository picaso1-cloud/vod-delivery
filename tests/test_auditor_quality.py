import asyncio
import json

from src import auditor


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(auditor, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(auditor, "FINDINGS_PATH", tmp_path / "findings.jsonl")
    monkeypatch.setattr(auditor, "NOTES_PATH", tmp_path / "NOTES.md")
    monkeypatch.setattr(auditor, "_db_signals", lambda ts: [])
    monkeypatch.setattr(auditor, "_structured_since", lambda iso: ([], [], []))


def test_known_quirk_filtered():
    assert auditor._is_known_quirk({"observation": "RemoteProtocolError interrupts the download"})
    assert auditor._is_known_quirk({"observation": "provider kills the connection every 40s"})
    assert not auditor._is_known_quirk({"observation": "TV refused every mode (cache/copy)"})


def test_normalized_key_ignores_numbers_keeps_feature():
    a = auditor._normalize_key("cast", "5 consecutive failures at 12:30")
    b = auditor._normalize_key("cast", "9 consecutive failures at 12:45")
    assert a == b
    assert auditor._normalize_key("cast", "x") != auditor._normalize_key("resume", "x")


def test_near_duplicate_suppressed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    base = {
        "severity": "medium",
        "feature": "casting",
        "observation": "cast failed after 20 seconds",
        "likely_cause": "c",
        "suggested_fix": "f",
    }
    assert len(auditor._note_finding([dict(base)])) == 1
    variant = dict(base, observation="cast failed after 42 seconds")
    assert auditor._note_finding([variant]) == []


def test_different_features_are_not_collapsed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    findings = [
        {"severity": "low", "feature": f"feature{i}", "observation": "same text", "likely_cause": "c", "suggested_fix": "f"}
        for i in range(4)
    ]
    assert len(auditor._note_finding(findings)) == 4


def test_quiet_window_skips_llm(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    log = tmp_path / "bot.log"
    log.write_text("2026-01-01 10:00:00,000 INFO vod-delivery: search index warmed up\n")
    monkeypatch.setattr(auditor, "LOG_PATH", log)
    (tmp_path / "state.json").write_text(json.dumps({"last_audit_ts": 0, "log_offset": 0}))

    async def boom(*args, **kwargs):
        raise AssertionError("LLM should not be called")

    monkeypatch.setattr(auditor, "_llm_audit", boom)
    assert asyncio.run(auditor.run_audit(force=False)) == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["quiet_skips"] == 1


def test_error_window_calls_llm(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    log = tmp_path / "bot.log"
    log.write_text("2026-01-01 10:00:00,000 ERROR vod-delivery: poll cycle failed\n")
    monkeypatch.setattr(auditor, "LOG_PATH", log)
    (tmp_path / "state.json").write_text(json.dumps({"last_audit_ts": 0, "log_offset": 0}))
    called = {}

    async def fake(events, db, structured, aggregates, failures):
        called["n"] = len(events)
        return []

    monkeypatch.setattr(auditor, "_llm_audit", fake)
    asyncio.run(auditor.run_audit(force=False))
    assert called["n"] >= 1


def test_download_interruptions_are_not_an_anomaly(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    log = tmp_path / "bot.log"
    log.write_text(
        "2026-01-01 10:00:00,000 WARNING vod-delivery: download interrupted at 0 bytes "
        "(+25690112 this round): RemoteProtocolError — resuming\n"
    )
    monkeypatch.setattr(auditor, "LOG_PATH", log)
    (tmp_path / "state.json").write_text(json.dumps({"last_audit_ts": 0, "log_offset": 0}))

    async def boom(*args, **kwargs):
        raise AssertionError("known quirk should not trigger the LLM")

    monkeypatch.setattr(auditor, "_llm_audit", boom)
    assert asyncio.run(auditor.run_audit(force=False)) == []


def test_llm_failure_keeps_the_window(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    log = tmp_path / "bot.log"
    log.write_text("2026-01-01 10:00:00,000 ERROR vod-delivery: poll cycle failed\n")
    monkeypatch.setattr(auditor, "LOG_PATH", log)
    (tmp_path / "state.json").write_text(json.dumps({"last_audit_ts": 1, "log_offset": 0}))

    async def fail(*args, **kwargs):
        return None

    monkeypatch.setattr(auditor, "_llm_audit", fail)
    assert asyncio.run(auditor.run_audit(force=False)) == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["log_offset"] == 0
    assert state["last_audit_ts"] == 1
