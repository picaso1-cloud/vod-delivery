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


def test_hex_ids_are_not_wedge_events():
    events = auditor._extract_events(
        "2026-01-01 10:00:00,000 WARNING vod-delivery: live source never produced data "
        "(7166142281c0, channel file 164055.ts) — offline\n"
    )
    assert [e["type"] for e in events] == ["raw"]


def test_interruption_storm_becomes_a_finding():
    events = [
        {"type": "dl_interrupted", "level": "WARNING", "msg": f"download interrupted at {i} bytes"}
        for i in range(auditor.RULE_MIN_INTERRUPTS)
    ]
    findings = auditor._rule_findings(events, [])
    assert [f["feature"] for f in findings] == ["re-stream proxy"]
    assert auditor._rule_findings(events[:3], []) == []


def test_slot_busy_burst_becomes_a_finding():
    busy = [
        {"ts": "2026-01-01T10:00:00", "component": "provider", "event": "slot_busy", "outcome": "refused"}
        for _ in range(auditor.RULE_MIN_SLOT_BUSY)
    ]
    findings = auditor._rule_findings([], busy)
    assert [f["feature"] for f in findings] == ["casting"]
    assert auditor._rule_findings([], busy[:1]) == []


def test_repeated_cast_failures_are_high():
    events = [
        {"type": "attempt_failed", "level": "WARNING", "msg": "attempt direct/copy failed: upnp error: 716"},
        {"type": "cast_refused", "level": "WARNING", "msg": "TV refused every mode"},
    ]
    findings = auditor._rule_findings(events, [])
    assert findings[0]["severity"] == "high"
    assert findings[0]["feature"] == "casting"


def test_plain_errors_become_findings_but_quirks_do_not():
    events = [
        {"type": "raw", "level": "ERROR", "msg": "poll cycle failed"},
        {"type": "raw", "level": "ERROR", "msg": "provider killed the connection"},
    ]
    findings = auditor._rule_findings(events, [])
    assert len(findings) == 1
    assert findings[0]["observation"] == "poll cycle failed"


def test_rule_findings_are_kept_without_the_llm(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    log = tmp_path / "bot.log"
    log.write_text("".join(
        f"2026-01-01 10:00:{i:02d},000 WARNING vod-delivery: download interrupted at {i} bytes "
        "(+1024 this round): RemoteProtocolError — resuming\n"
        for i in range(auditor.RULE_MIN_INTERRUPTS)
    ))
    monkeypatch.setattr(auditor, "LOG_PATH", log)
    (tmp_path / "state.json").write_text(json.dumps({"last_audit_ts": 0, "log_offset": 0}))

    async def boom(*args, **kwargs):
        raise AssertionError("LLM must not be needed for a rule finding")

    monkeypatch.setattr(auditor, "_llm_audit", boom)
    fresh = asyncio.run(auditor.run_audit(force=False))
    assert len(fresh) == 1
    assert fresh[0]["feature"] == "re-stream proxy"
