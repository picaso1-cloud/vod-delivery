import hashlib
import json
import logging
import re
import time
from pathlib import Path

import httpx

from .config import LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger("vod-delivery")

AUDIT_DIR = Path("data/audit")
STATE_PATH = AUDIT_DIR / "state.json"
FINDINGS_PATH = AUDIT_DIR / "findings.jsonl"
NOTES_PATH = AUDIT_DIR / "NOTES.md"
LOG_PATH = Path("data/bot.log")
MAX_LOG_BYTES = 60_000
MAX_EVENTS = 120
MAX_FINDINGS_PER_RUN = 5
RECURRENCE_ESCALATE_AT = (5, 15, 40)
SEVERITY_RANK = {"high": 2, "medium": 1, "low": 0}

FEATURES_MANIFEST = """\
- natural-language search: user sends text -> LLM parses intent -> poster cards or direct episode
  offer. NORMAL: a 'parsed' log line within seconds and search results. ANOMALY: nonsense titles
  searched for feedback/questions, 'LLM error', no 'parsed' line for a text message.
- casting: after TV pick, a cast attempt log 'cast attempt <mode> -> <state>'. NORMAL: ends PLAYING.
  ANOMALY: STOPPED, 701/716 wedged errors, repeated fallbacks to later modes, TV unreachable.
- resume: 'continue' should start at last position ('Started at'/'Resumed at' or file offset start).
  ANOMALY: playback restarting from 0 after a resume request.
- watch progress tracking: poller updates position every 30s while playing.
  ANOMALY: position stuck at same value across polls, duration always 0 when mode supports it.
- watchlist auto-download: queue items download one at a time in idle time, 15-20 min apart,
  files land organized under data/library/shows|movies. ANOMALY: 'download interrupted' storms,
  queue stuck 'downloading', 'failed' with metadata missing, duplicates of the same episode.
- re-stream proxy/file mode: serves the TV locally; provider connection kills are auto-resumed.
  ANOMALY: 'download interrupted' every cycle (provider hostile to our UA), empty ffmpeg logs with
  0-byte files, TV fetches that never happen.
- library/cache: downloads finalize into the library index with real sizes; cache usage should stay
  under the configured limit. ANOMALY: orphan files growing with no viewer, finalize failures.
- telegram UX: every user action gets an answer. ANOMALY: 'query is too old', 'message is not
  modified' storms (double taps are ok but storms suggest stuck flows), 'not handled' updates.
- provider quirks (known, do NOT report): max 1 connection, connections may be killed and the
  downloader auto-resumes, HEAD requests may fail, :88 streams redirect.\
"""

AUDIT_SYSTEM = """You are the self-audit agent of a home IPTV casting service. You receive:
1. A manifest describing each feature's goal and its normal/abnormal signals.
2. A window of raw service events since the last audit.

Analyze ONLY this window against the manifest. Keep your reasoning VERY brief (a few short
lines maximum — token budget is tight). Report real anomalies, failures, illogical flows
and inconsistencies. Do NOT report known provider quirks. Do NOT invent issues. If everything is
normal, return an empty findings list.

Reply with brief reasoning first, then ONE JSON object as the last thing in your output:
{"findings": [{"severity": "low|medium|high", "feature": "...", "observation": "...",
"likely_cause": "...", "suggested_fix": "..."}]}
"""

EVENT_PATTERNS = [
    ("parse", re.compile(r"parsed '(.+?)' → (\{.*\})")),
    ("cast_attempt", re.compile(r"cast attempt (\S+) → (\S+)")),
    ("attempt_failed", re.compile(r"attempt (\S+) failed: (.*)")),
    ("dl_interrupted", re.compile(r"download interrupted at (\d+) bytes \(\+(\d+) this round\)")),
    ("dl_done", re.compile(r"library download done: (.+) \((.+) GB\)")),
    ("dl_start", re.compile(r"library download starting: (.+)")),
    ("dl_idle", re.compile(r"download idle|session idle")),
    ("wedge", re.compile(r"DLNA stack looks wedged|701|716")),
    ("tg_error", re.compile(r"query is too old|message is not modified|not handled")),
    ("poster_fail", re.compile(r"poster send failed for (.+?);")),
    ("poll_fail", re.compile(r"poll cycle failed")),
    ("viewing_defer", re.compile(r"viewing started — aborting library download")),
]


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"last_audit_ts": 0, "log_offset": 0, "recent_hashes": [], "recurrence": {}}


def _save_state(state: dict) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    state["recent_hashes"] = state.get("recent_hashes", [])[-200:]
    live = set(state["recent_hashes"])
    state["recurrence"] = {h: v for h, v in state.get("recurrence", {}).items() if h in live}
    STATE_PATH.write_text(json.dumps(state))


def _read_log_window(state: dict) -> tuple[str, int]:
    if not LOG_PATH.exists():
        return "", state.get("log_offset", 0)
    size = LOG_PATH.stat().st_size
    offset = state.get("log_offset", 0)
    if offset >= size:
        return "", size
    tail_mode = offset == 0 and size > MAX_LOG_BYTES
    start = size - MAX_LOG_BYTES if tail_mode else offset
    with LOG_PATH.open("rb") as fh:
        fh.seek(start)
        data = fh.read(MAX_LOG_BYTES if tail_mode else size - offset)
    if data:
        data = data[data.find(b"\n") + 1:]
    return data.decode("utf-8", "replace"), size


def _extract_events(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if not line or "httpx" in line or "huggingface" in line or "aiohttp.access" in line:
            continue
        m = re.match(r"^(\S+ \S+) (\S+) (\S+): (.*)", line)
        if not m:
            continue
        ts, level, name, msg = m.groups()
        if name.startswith("aiogram.event") or name.startswith("aiogram.dispatcher"):
            continue
        ev = {"ts": ts, "level": level, "type": "raw", "msg": msg[:220]}
        for pname, pattern in EVENT_PATTERNS:
            pm = pattern.search(msg)
            if pm:
                ev = {"ts": ts, "level": level, "type": pname, "msg": msg[:220], "groups": [g[:120] for g in pm.groups()]}
                break
        events.append(ev)
    return events[-MAX_EVENTS:]


def _db_signals(since_ts: float) -> list[dict]:
    from . import store

    out = []
    try:
        conn = store.connect()
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_ts))
        for r in conn.execute(
            "SELECT title, tv_name, position_s, duration_s, started_at FROM plays WHERE started_at >= ? ORDER BY id DESC LIMIT 10",
            (since,),
        ):
            out.append({"type": "play", "title": r["title"], "tv": r["tv_name"],
                        "pos": round(r["position_s"] or 0), "dur": round(r["duration_s"] or 0),
                        "at": r["started_at"]})
        for r in conn.execute(
            "SELECT title, status, error FROM download_queue WHERE added_at >= ? ORDER BY id DESC LIMIT 10",
            (since,),
        ):
            out.append({"type": "queue", "title": r["title"], "status": r["status"], "error": r["error"]})
        failed = conn.execute("SELECT COUNT(*) AS c FROM download_queue WHERE status='failed'").fetchone()["c"]
        out.append({"type": "queue_failed_total", "count": failed})
        conn.close()
    except Exception:
        logger.exception("audit db signals failed")
    return out


async def _llm_audit(events: list[dict], db_signals: list[dict], structured: list[dict] | None = None) -> list[dict]:
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": AUDIT_SYSTEM + "\n\nFEATURES MANIFEST:\n" + FEATURES_MANIFEST},
            {"role": "user", "content": "EVENT WINDOW (JSON):\n" + json.dumps(events, ensure_ascii=False)
             + "\n\nDB SIGNALS (JSON):\n" + json.dumps(db_signals, ensure_ascii=False)
             + ("\n\nSTRUCTURED EVENTS (JSON):\n" + json.dumps(structured, ensure_ascii=False) if structured else "")},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(f"{LLM_BASE_URL}/chat/completions", json=payload)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    parsed = _extract_balanced_json(content, "findings")
    if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
        return [f for f in parsed["findings"] if isinstance(f, dict) and f.get("observation")]
    logger.warning("audit LLM returned no usable findings JSON; tail: %r", content[-300:])
    return []


def _extract_balanced_json(content: str, key: str):
    key_pos = content.find(f'"{key}"')
    if key_pos < 0:
        return None
    start = content.rfind("{", 0, key_pos)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(content)):
        ch = content[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(content[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _bump_severity(sev: str) -> str:
    order = ["low", "medium", "high"]
    try:
        return order[min(order.index(sev) + 1, len(order) - 1)]
    except ValueError:
        return "medium"


def _note_finding(findings: list[dict]) -> list[dict]:
    state = _state()
    recent = list(state.get("recent_hashes", []))
    recurrence = state.get("recurrence", {})
    ranked = sorted(
        findings,
        key=lambda f: -SEVERITY_RANK.get(str(f.get("severity", "low")).lower(), 0),
    )
    overflow = ranked[MAX_FINDINGS_PER_RUN:]
    if overflow:
        logger.info("audit capped %d finding(s) beyond per-run limit", len(overflow))
    fresh = []
    for f in ranked[:MAX_FINDINGS_PER_RUN]:
        if not isinstance(f.get("severity"), str) or f["severity"].lower() not in SEVERITY_RANK:
            f["severity"] = "low"
        key = f"{f.get('feature','?')}|{f.get('observation','')[:120]}"
        h = hashlib.sha1(key.encode()).hexdigest()[:12]
        if h in recent:
            rec = recurrence.get(h, {"c": 1, "last": ""})
            rec["c"] += 1
            rec["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
            recurrence[h] = rec
            if rec["c"] in RECURRENCE_ESCALATE_AT:
                f = {
                    **f,
                    "hash": h,
                    "severity": _bump_severity(f["severity"]),
                    "observation": f"{f.get('observation')} [recurred ×{rec['c']}]",
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                fresh.append(f)
            continue
        recent.append(h)
        recurrence[h] = {"c": 1, "last": time.strftime("%Y-%m-%d %H:%M:%S")}
        f = {**f, "hash": h, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        fresh.append(f)
    state["recent_hashes"] = recent
    state["recurrence"] = recurrence
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    for f in fresh:
        with FINDINGS_PATH.open("a") as fh:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")
    _save_state(state)
    if fresh:
        with NOTES_PATH.open("a") as fh:
            fh.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n")
            for f in fresh:
                fh.write(
                    f"- [{f.get('severity','?').upper()}] {f.get('feature','?')}: {f.get('observation')}\n"
                    f"  - cause: {f.get('likely_cause','?')}\n  - fix: {f.get('suggested_fix','?')}\n"
                )
    return fresh


async def run_audit(force: bool = False) -> list[dict]:
    state = _state()
    now = time.time()
    if not force and now - state.get("last_audit_ts", 0) < 600:
        return []
    raw, new_offset = _read_log_window(state)
    events = _extract_events(raw)
    db_signals = _db_signals(state.get("last_audit_ts", 0))
    if not force and not events and not db_signals:
        return []
    state["last_audit_ts"] = now
    state["log_offset"] = new_offset
    _save_state(state)
    from .telemetry import events_since
    iso_since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(state.get("last_audit_ts", 0)))
    structured = events_since(iso_since)
    if structured:
        logger.info("audit including %d structured events", len(structured))
    findings = await _llm_audit(events, db_signals, structured)
    fresh = _note_finding(findings)
    logger.info("audit finished: %d findings (%d new)", len(findings), len(fresh))
    return fresh
