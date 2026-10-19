import json
import threading
import time
from pathlib import Path

EVENTS_PATH = Path("data/audit/events.jsonl")
_lock = threading.Lock()


def emit(component: str, event: str, outcome: str = "ok", **details) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "component": component,
        "event": event,
        "outcome": outcome,
    }
    if details:
        record["details"] = {k: v for k, v in details.items() if v is not None}
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with EVENTS_PATH.open("a") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def events_since(iso_ts: str, limit: int = 200) -> list[dict]:
    try:
        lines = EVENTS_PATH.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("ts", "") >= iso_ts:
            out.append(record)
    return out[-limit:]
