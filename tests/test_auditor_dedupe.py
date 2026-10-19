import json

from src import auditor


def _seed_state(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(auditor, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(auditor, "FINDINGS_PATH", tmp_path / "findings.jsonl")
    monkeypatch.setattr(auditor, "NOTES_PATH", tmp_path / "NOTES.md")


def test_new_finding_recorded_once(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    f = [{"severity": "high", "feature": "casting", "observation": "TV refused every mode", "likely_cause": "x", "suggested_fix": "y"}]
    fresh1 = auditor._note_finding([dict(f[0])])
    assert len(fresh1) == 1
    fresh2 = auditor._note_finding([dict(f[0])])
    assert fresh2 == []
    lines = (tmp_path / "findings.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_recurrence_escalates_at_threshold(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    base = {"severity": "low", "feature": "proxy", "observation": "kills every cycle", "likely_cause": "c", "suggested_fix": "f"}
    assert len(auditor._note_finding([dict(base)])) == 1
    for _ in range(3):
        assert auditor._note_finding([dict(base)]) == []
    escalated = auditor._note_finding([dict(base)])
    assert len(escalated) == 1
    assert "recurred ×5" in escalated[0]["observation"]
    assert escalated[0]["severity"] == "medium"


def test_per_run_cap_keeps_highest_severity(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    findings = [
        {"severity": "low", "feature": f"f{i}", "observation": f"obs {i}", "likely_cause": "c", "suggested_fix": "f"}
        for i in range(8)
    ]
    findings.append({"severity": "high", "feature": "critical", "observation": "big one", "likely_cause": "c", "suggested_fix": "f"})
    fresh = auditor._note_finding(findings)
    assert len(fresh) == auditor.MAX_FINDINGS_PER_RUN
    assert any(f["feature"] == "critical" for f in fresh)


def test_unknown_severity_normalized(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    fresh = auditor._note_finding([{"severity": "catastrophic", "feature": "x", "observation": "obs", "likely_cause": "c", "suggested_fix": "f"}])
    assert fresh[0]["severity"] == "low"


def test_recurrence_pruned_from_state(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    base = {"severity": "low", "feature": "p", "observation": "o", "likely_cause": "c", "suggested_fix": "f"}
    auditor._note_finding([dict(base)])
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["recurrence"], "recurrence should be persisted"
    (tmp_path / "state.json").write_text(json.dumps({"last_audit_ts": 0, "log_offset": 0, "recent_hashes": [], "recurrence": state["recurrence"]}))
    auditor._note_finding([{"severity": "low", "feature": "other", "observation": "o2", "likely_cause": "c", "suggested_fix": "f"}])
    state2 = json.loads((tmp_path / "state.json").read_text())
    assert all(h in set(state2["recent_hashes"]) for h in state2["recurrence"])
