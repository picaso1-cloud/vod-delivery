import asyncio
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store
from src.parsing import normalize_for_grouping
from src.provider import Provider

SNAP_DIR = Path("data/catalog-snapshots")
KEEP_SNAPSHOTS = 5


def _norm(title: str) -> str:
    return normalize_for_grouping(title or "")


async def fetch_snapshot(provider: Provider) -> dict:
    movies = await provider.vod_streams(fresh=True)
    series = await provider.series(fresh=True)
    catalog = defaultdict(list)
    for m in movies or []:
        catalog[("movie", _norm(m.get("name")))].append(int(m["stream_id"]))
    for s in series or []:
        catalog[("series", _norm(s.get("name")))].append(int(s["series_id"]))
    return {f"{kind}|{key}": sorted(ids) for (kind, key), ids in catalog.items()}


def snapshot_fingerprint(snapshot: dict) -> str:
    canonical = json.dumps(snapshot, sort_keys=True).encode()
    return hashlib.sha1(canonical).hexdigest()[:12]


def save_snapshot(snapshot: dict, label: str) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = SNAP_DIR / f"snapshot-{label}-{stamp}.json"
    payload = {
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "label": label,
        "fingerprint": snapshot_fingerprint(snapshot),
        "catalog": snapshot,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False))
    latest = SNAP_DIR / f"latest-{label}.json"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(path.name)
    snaps = sorted(SNAP_DIR.glob(f"snapshot-{label}-*.json"))
    for old in snaps[:-KEEP_SNAPSHOTS]:
        old.unlink(missing_ok=True)
    return path


def load_latest(label: str) -> dict | None:
    path = SNAP_DIR / f"latest-{label}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["catalog"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def diff_snapshots(old: dict, new: dict) -> dict:
    old_keys, new_keys = set(old), set(new)
    surviving, moved, missing, new_keys_only = [], [], [], []
    for key in old_keys & new_keys:
        if old[key] == new[key]:
            surviving.append(key)
        else:
            moved.append({"key": key, "old_ids": old[key], "new_ids": new[key]})
    for key in old_keys - new_keys:
        missing.append({"key": key, "old_ids": old[key]})
    for key in new_keys - old_keys:
        new_keys_only.append({"key": key, "new_ids": new[key]})
    total_old = len(old_keys)
    churn = round(100 * (len(moved) + len(missing) + len(new_keys_only)) / total_old, 2) if total_old else 0.0
    return {
        "surviving": len(surviving),
        "moved": moved,
        "missing": missing,
        "new": new_keys_only,
        "churn_percent": churn,
        "total_old": total_old,
        "total_new": len(new_keys),
    }


def reconcile_library(snapshot: dict) -> dict:
    conn = store.connect()
    known: dict[str, set] = defaultdict(set)
    for key, ids in snapshot.items():
        kind = key.split("|", 1)[0]
        for i in ids:
            known[kind].add(i)
    results = {"present": [], "vanished": []}
    try:
        rows = conn.execute(
            "SELECT l.media_key, l.kind, l.ref_id, l.title, e.series_id AS parent_series"
            " FROM library l LEFT JOIN episodes e ON e.episode_id = l.ref_id"
            " WHERE l.status IN ('ready','archived') AND l.title NOT LIKE '\_%' ESCAPE '\\'"
        ).fetchall()
        for row in rows:
            entry = {"media_key": row["media_key"], "title": row["title"], "ref_id": row["ref_id"]}
            if row["kind"] == "episode":
                ok = row["parent_series"] in known["series"]
            elif row["kind"] == "series":
                ok = row["ref_id"] in known["series"]
            else:
                ok = row["ref_id"] in known["movie"]
            (results["present"] if ok else results["vanished"]).append(entry)
    finally:
        conn.close()
    return results


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "diff"
    label = sys.argv[2] if len(sys.argv) > 2 else "main"
    provider = Provider()
    try:
        current = await fetch_snapshot(provider)
    finally:
        await provider.close()
    print(f"current catalog: {len(current)} normalized keys, fingerprint {snapshot_fingerprint(current)}")

    if mode == "snapshot":
        path = save_snapshot(current, label)
        print(f"snapshot saved: {path}")
        return

    previous = load_latest(label)
    if previous is None:
        print("no previous snapshot — saving current as baseline (run again after renewal to diff)")
        save_snapshot(current, label)
        return

    report = diff_snapshots(previous, current)
    recon = reconcile_library(current)
    print(f"\n=== CATALOG DIFF vs latest-{label} ===")
    print(f"surviving: {report['surviving']}/{report['total_old']}")
    print(f"moved (same title, new ids): {len(report['moved'])}")
    print(f"missing from provider: {len(report['missing'])}")
    print(f"new on provider: {len(report['new'])}")
    print(f"CHURN: {report['churn_percent']}%")
    print(f"\n=== LIBRARY RECONCILIATION ===")
    print(f"library items still on provider: {len(recon['present'])}")
    print(f"library items VANISHED from provider: {len(recon['vanished'])}")
    for item in recon["vanished"][:15]:
        print(f"  - {item['title']} (ref {item['ref_id']}) — archive copy still playable locally")
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / "last-diff.json").write_text(json.dumps(
        {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label, "diff_summary": {
            k: (v if not isinstance(v, list) else len(v)) for k, v in report.items()},
         "library": {"present": len(recon["present"]), "vanished": recon["vanished"]}}, ensure_ascii=False, indent=1))
    print(f"\nsaved: {SNAP_DIR / 'last-diff.json'}  (new baseline NOT saved — run 'snapshot' to accept)")


if __name__ == "__main__":
    asyncio.run(main())
