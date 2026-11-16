import hashlib
import json
import logging
import re
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from . import store
from .config import LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger("vod-delivery")

AUDIT_DIR = Path("data/audit")
STATE_PATH = AUDIT_DIR / "state.json"
FINDINGS_PATH = AUDIT_DIR / "findings.jsonl"
NOTES_PATH = AUDIT_DIR / "NOTES.md"
LOG_PATH = Path("data/bot.log")
MAX_LOG_BYTES = 60_000
MAX_EVENTS = 80
MAX_FINDINGS_PER_RUN = 5
MAX_STRUCTURED = 40
RECURRENCE_ESCALATE_AT = (5, 15, 40)
SEVERITY_RANK = {"high": 2, "medium": 1, "low": 0}
NOTIFY_COOLDOWN_S = 6 * 3600
RECENT_KEYS = 200
SIMILARITY = 0.88

FEATURES_MANIFEST = """\
- natural-language search: user sends text -> LLM parses intent -> poster cards or direct episode
  offer. NORMAL: a 'parsed' log line within seconds and search results. ANOMALY: nonsense titles
  searched for feedback/questions, 'LLM error', no 'parsed' line for a text message.
- casting: 'attempt <mode>/<quality> failed|aborted', 'TV refused every mode', or a telemetry
  cast/attempt event with outcome failed. NORMAL: an attempt ends PLAYING and the session keeps
  serving. ANOMALY: STOPPED, 701/716 wedged errors, every mode failing, TV unreachable.
- resume: 'continue' should start at last position ('Started at'/'Resumed at' or file offset start).
  ANOMALY: playback restarting from 0 after a resume request.
- watch progress tracking: poller updates position every 30s while playing.
  ANOMALY: position stuck at same value across polls, duration always 0 when mode supports it.
- watchlist auto-download: queue items download one at a time in idle time, 15-20 min apart,
  files land organized under data/library/shows|movies. ANOMALY: queue stuck 'downloading',
  'failed' with metadata missing, duplicates of the same episode.
- re-stream proxy/file mode: serves the TV locally. ANOMALY (non-quirk): sessions that never
  produce bytes, TV fetches that never happen, files finalized with size 0.
- library/cache: downloads finalize into the library index with real sizes; cache usage should stay
  under the configured limit. ANOMALY: orphan files growing with no viewer, finalize failures.
- telegram UX: every user action gets an answer. ANOMALY: 'not handled' updates, stuck flows.
- app API: requests should answer quickly; sessions should be controllable. ANOMALY: 5xx storms,
  sessions that refuse stop/seek, stale sessions blocking a new cast.
- known, do NOT report: provider kills long-lived streams (RemoteProtocolError / 'download
  interrupted' followed by a resumed ranged GET), HEAD/range probes failing, max 1 provider
  connection, :88 streams redirecting, DLNA control-plane timeouts while a TV is decoding,
  the neighbour LG on 192.168.1.151.\
"""

AUDIT_SYSTEM = """You are the self-audit agent of a home IPTV casting service. You receive:
1. A manifest describing each feature's goal and its normal/abnormal signals.
2. Notable events from the service window (warnings/errors + typed events).
3. DB signals and structured telemetry aggregates.

Analyze ONLY this window against the manifest. Keep reasoning VERY brief (a few short lines).
Report only issues supported by a concrete event; put that log line or telemetry entry in
"evidence". Do NOT report the known quirks listed in the manifest. Do NOT invent issues.
If everything is normal, return an empty findings list.

Reply with brief reasoning first, then ONE JSON object as the last thing in your output:
{"findings": [{"severity": "low|medium|high", "feature": "...", "observation": "...",
"evidence": "...", "likely_cause": "...", "suggested_fix": "..."}]}
"""

EVENT_PATTERNS = [
    ("parse", re.compile(r"parsed '(.+?)' → (\{.*\})")),
    ("attempt_failed", re.compile(r"attempt (\S+)/(\S+) failed: (.*)")),
    ("cast_refused", re.compile(r"TV refused every mode|control plane unreliable|DLNA stack looks wedged|aborting cast")),
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

KNOWN_QUIRK_RE = [
    re.compile(r"provider (connection )?kill|hostile user[- ]agent|hostile ua|remote ?protocol ?error", re.I),
    re.compile(r"head (request|probe).*(fail|refus)|accept-ranges|range.*ignored|end[- ]probe", re.I),
    re.compile(r"max_connections|single (provider )?connection|one connection|only one stream", re.I),
    re.compile(r"redirect|:88\b", re.I),
]

SEVERITY_LEVELS = ("WARNING", "ERROR", "CRITICAL")
BAD_EVENT_TYPES = ("attempt_failed", "cast_refused", "poll_fail")


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {
            "last_audit_ts": 0,
            "log_offset": 0,
            "recent_hashes": [],
            "recent_keys": [],
            "recurrence": {},
            "notified": {},
            "runs": 0,
            "quiet_skips": 0,
        }


def _save_state(state: dict) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    state["recent_hashes"] = state.get("recent_hashes", [])[-RECENT_KEYS:]
    state["recent_keys"] = state.get("recent_keys", [])[-RECENT_KEYS:]
    live = set(state["recent_hashes"])
    state["recurrence"] = {h: v for h, v in state.get("recurrence", {}).items() if h in live}
    now = time.time()
    state["notified"] = {
        h: ts for h, ts in state.get("notified", {}).items()
        if h in live and now - float(ts) < NOTIFY_COOLDOWN_S * 4
    }
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
    if data and start > 0:
        data = data[data.find(b"\n") + 1:]
    return data.decode("utf-8", "replace"), size


def _extract_events(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if not line or "httpx" in line or "huggingface" in line or "aiohttp.access" in line:
            continue
        if any(noise in line for noise in (
            "ClientConnectionResetError", "Cannot write to closing transport",
            "BrokenPipeError", "ConnectionResetError", "SocketTimeoutException",
            "child process pid", "exit status already read", "Keystore",
        )):
            continue
        m = re.match(r"^(\S+ \S+) (\S+) (\S+): (.*)", line)
        if not m:
            continue
        ts, level, name, msg = m.groups()
        if name.startswith("aiogram.event") or name.startswith("aiogram.dispatcher"):
            continue
        ev = None
        for pname, pattern in EVENT_PATTERNS:
            pm = pattern.search(msg)
            if pm:
                ev = {"ts": ts, "level": level, "type": pname, "msg": msg[:220],
                      "groups": [g[:120] for g in pm.groups()]}
                break
        if ev is None:
            if level not in SEVERITY_LEVELS:
                continue
            ev = {"ts": ts, "level": level, "type": "raw", "msg": msg[:220]}
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
            "SELECT title, status, error, added_at FROM download_queue WHERE added_at >= ? ORDER BY id DESC LIMIT 10",
            (since,),
        ):
            out.append({"type": "queue", "title": r["title"], "status": r["status"], "error": r["error"]})
        failed = conn.execute("SELECT COUNT(*) AS c FROM download_queue WHERE status='failed'").fetchone()["c"]
        stuck = conn.execute(
            "SELECT COUNT(*) AS c FROM download_queue WHERE status='downloading' AND added_at < ?",
            (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600)),),
        ).fetchone()["c"]
        out.append({"type": "queue_health", "failed": failed, "stuck": stuck})
        conn.close()
    except Exception:
        logger.exception("audit db signals failed")
    return out


def _structured_since(iso_ts: str) -> tuple[list[dict], list[dict], list[dict]]:
    from .telemetry import events_since

    try:
        events = events_since(iso_ts)
    except Exception:
        return [], [], []
    failures = [e for e in events if str(e.get("outcome", "")) in ("failed", "aborted", "error")]
    counts = Counter((e.get("component", "?"), e.get("event", "?"), e.get("outcome", "?")) for e in events)
    aggregates = [
        {"component": c, "event": v, "outcome": o, "count": n}
        for (c, v, o), n in counts.most_common(12)
    ]
    return events[-MAX_STRUCTURED:], aggregates, failures[-10:]


def _is_known_quirk(finding: dict) -> bool:
    blob = " ".join(str(finding.get(k, "")) for k in ("observation", "likely_cause", "suggested_fix"))
    return any(rx.search(blob) for rx in KNOWN_QUIRK_RE)


def _quiet(events: list[dict], db_signals: list[dict], structured_failures: list[dict]) -> bool:
    if structured_failures:
        return False
    for e in events:
        etype = e.get("type")
        if etype in ("dl_interrupted", "dl_idle"):
            continue
        if e.get("level") in SEVERITY_LEVELS:
            return False
        msg = e.get("msg", "")
        if etype in BAD_EVENT_TYPES:
            return False
        if etype == "cast_refused":
            return False
        if "control plane unreliable" in msg or "took too long" in msg:
            return False
    failed = sum(1 for s in db_signals if s["type"] == "queue" and s.get("status") == "failed")
    stuck = sum(s.get("stuck", 0) for s in db_signals if s["type"] == "queue_health")
    zero_plays = sum(1 for s in db_signals if s["type"] == "play" and (s.get("dur") or 0) == 0)
    return failed == 0 and stuck == 0 and zero_plays < 2


async def _llm_audit(events: list[dict], db_signals: list[dict], structured: list[dict],
                     aggregates: list[dict], failures: list[dict]) -> list[dict] | None:
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": AUDIT_SYSTEM + "\n\nFEATURES MANIFEST:\n" + FEATURES_MANIFEST},
            {"role": "user", "content": "EVENTS (JSON):\n" + json.dumps(events, ensure_ascii=False)
             + "\n\nDB SIGNALS (JSON):\n" + json.dumps(db_signals, ensure_ascii=False)
             + "\n\nTELEMETRY AGGREGATES (JSON):\n" + json.dumps(aggregates, ensure_ascii=False)
             + ("\n\nSTRUCTURED FAILURES (JSON):\n" + json.dumps(failures, ensure_ascii=False) if failures else "")
             + ("\n\nSTRUCTURED EVENTS (JSON):\n" + json.dumps(structured, ensure_ascii=False) if structured else "")},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(f"{LLM_BASE_URL}/chat/completions", json=payload)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            content = message.get("content") or message.get("reasoning_content") or ""
    except Exception as exc:
        logger.warning("audit LLM call failed (%s: %s) — keeping the window for the next cycle",
                       type(exc).__name__, str(exc)[:160])
        return None
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    parsed = _extract_balanced_json(content, "findings")
    if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
        return [
            f for f in parsed["findings"]
            if isinstance(f, dict) and f.get("observation") and not _is_known_quirk(f)
        ]
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


def _normalize_key(feature: str, observation: str) -> str:
    feat = re.sub(r"\s+", " ", str(feature).lower()).strip()
    blob = str(observation).lower()
    blob = re.sub(r"0x[0-9a-f]+|[0-9a-f]{10,}", "#", blob)
    blob = re.sub(r"\d+(?:\.\d+)?", "#", blob)
    blob = re.sub(r"\s+", " ", blob).strip()
    return f"{feat}|{blob}"[:140]


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY


def _note_finding(findings: list[dict]) -> list[dict]:
    state = _state()
    recent = list(state.get("recent_hashes", []))
    recent_keys = list(state.get("recent_keys", []))
    recurrence = state.get("recurrence", {})
    notified = state.get("notified", {})
    now = time.time()
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
        key = _normalize_key(str(f.get("feature", "?")), str(f.get("observation", "")))
        h = hashlib.sha1(key.encode()).hexdigest()[:12]
        if h not in recent:
            prefix = key.split("|", 1)[0] + "|"
            similar = next(
                (k for k in recent_keys[-60:] if k.startswith(prefix) and _similar(key, k)),
                None,
            )
            if similar is not None:
                h = hashlib.sha1(similar.encode()).hexdigest()[:12]
                key = similar
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
        recent_keys.append(key)
        recurrence[h] = {"c": 1, "last": time.strftime("%Y-%m-%d %H:%M:%S")}
        f = {**f, "hash": h, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        fresh.append(f)
    for f in fresh:
        if str(f.get("severity", "")).lower() == "high":
            try:
                conn = store.connect()
                store.add_event(
                    conn, "audit", str(f.get("feature") or "audit"),
                    str(f.get("observation") or "")[:300],
                )
                conn.close()
            except Exception:
                logger.debug("audit event write failed", exc_info=True)
        h = f.get("hash", "")
        f["notify"] = bool(now - float(notified.get(h, 0)) > NOTIFY_COOLDOWN_S)
        if f["notify"]:
            notified[h] = now
    state["recent_hashes"] = recent
    state["recent_keys"] = recent_keys
    state["recurrence"] = recurrence
    state["notified"] = notified
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
                    f"  - evidence: {f.get('evidence','—')}\n"
                    f"  - cause: {f.get('likely_cause','?')}\n  - fix: {f.get('suggested_fix','?')}\n"
                )
    return fresh


def stats() -> dict:
    state = _state()
    return {
        "runs": state.get("runs", 0),
        "quiet_skips": state.get("quiet_skips", 0),
        "last_audit": state.get("last_audit_ts", 0),
        "findings": state.get("recent_hashes", []).__len__(),
        "recurring": sum(1 for v in state.get("recurrence", {}).values() if v.get("c", 1) > 1),
    }


async def run_audit(force: bool = False) -> list[dict]:
    state = _state()
    now = time.time()
    if not force and now - state.get("last_audit_ts", 0) < 600:
        return []
    raw, new_offset = _read_log_window(state)
    events = _extract_events(raw)
    db_signals = _db_signals(state.get("last_audit_ts", 0))
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(state.get("last_audit_ts", 0)))
    structured, aggregates, failures = _structured_since(since_iso)
    prev_ts = state.get("last_audit_ts", 0)
    prev_offset = state.get("log_offset", 0)
    state["last_audit_ts"] = now
    state["log_offset"] = new_offset
    state["runs"] = state.get("runs", 0) + 1
    if not force and _quiet(events, db_signals, failures):
        state["quiet_skips"] = state.get("quiet_skips", 0) + 1
        _save_state(state)
        logger.info("audit quiet: no notable events (%d events, %d telemetry) — LLM skipped",
                    len(events), len(structured))
        return []
    _save_state(state)
    findings = await _llm_audit(events, db_signals, structured, aggregates, failures)
    if findings is None:
        state["last_audit_ts"] = prev_ts
        state["log_offset"] = prev_offset
        _save_state(state)
        return []
    fresh = _note_finding(findings)
    logger.info("audit finished: %d findings (%d new), %d events, %d telemetry",
                len(findings), len(fresh), len(events), len(structured))
    return fresh
