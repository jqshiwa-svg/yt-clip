"""Code-enforced safety gates that run on every scheduler tick, independent of the agents."""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

from . import takedowns
from .config import Config
from .logs import alert, log
from .media import probe_duration, read_sidecar
from .permissions import Permissions
from .queue import Queue

UNPUBLISHED = ("idea", "scripted", "edited", "approved")


def _state(cfg: Config) -> dict:
    p = cfg.paths.logs / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(cfg: Config, st: dict) -> None:
    (cfg.paths.logs / "state.json").write_text(json.dumps(st, indent=2))


def source_duration(cfg: Config, source_file: str | None) -> float | None:
    if not source_file:
        return None
    f = cfg.paths.raw / Path(source_file).name
    if not f.exists():
        return None
    meta = read_sidecar(f)
    if meta.get("duration"):
        return float(meta["duration"])
    return probe_duration(f)


def reject_unapproved(cfg: Config, q: Queue, perms: Permissions) -> list[int]:
    """Any unpublished row whose creator is no longer approved is rejected at once."""
    rejected = []
    for status in UNPUBLISHED:
        for row in q.by_status(status):
            c = perms.get(row["creator"])
            if c and c.is_approved():
                continue
            why = f"creator status is now '{c.status if c else 'missing'}'"
            q.transition("safety", row["id"], "rejected", note=why, review_notes=f"[safety] {why}", rejected_by="safety")
            rejected.append(row["id"])
    if rejected:
        log(cfg, "safety", "rejected_unapproved", rows=rejected)
    return rejected


def reject_bad_bounds(cfg: Config, q: Queue) -> list[int]:
    rejected = []
    for status in UNPUBLISHED:
        for row in q.by_status(status):
            if row["clip_start"] is None or row["clip_end"] is None or not row["source_file"]:
                continue
            dur = source_duration(cfg, row["source_file"])
            a, b = float(row["clip_start"]), float(row["clip_end"])
            if dur is None or a < 0 or b <= a or b > dur + 0.01:
                why = f"clip {a}-{b} is outside the source duration ({dur})"
                q.transition("safety", row["id"], "rejected", note=why, review_notes=f"[safety] {why}", rejected_by="safety")
                rejected.append(row["id"])
    if rejected:
        log(cfg, "safety", "rejected_bounds", rows=rejected)
    return rejected


def alert_removed_creators(cfg: Config, perms: Permissions) -> None:
    """Escalate the first time we see a creator move to paused or removed."""
    st = _state(cfg)
    seen = st.get("creator_status", {})
    for c in perms.creators:
        old = seen.get(c.key)
        if old and old != c.status and c.status in ("paused", "removed"):
            kind = "Creator removed" if c.status == "removed" else "Creator paused"
            alert(cfg, kind, f"{c.creator} changed from {old} to {c.status}. Their unpublished rows were rejected. "
                             f"Check brain/permissions.md and brain/takedowns.md.")
        seen[c.key] = c.status
    st["creator_status"] = seen
    _save_state(cfg, st)


def takedown_block(cfg: Config) -> list[str]:
    """Returns unresolved takedown entries; alerts once per day while any are open."""
    open_items = takedowns.unresolved(cfg.paths.takedowns)
    if open_items:
        st = _state(cfg)
        if st.get("takedown_alerted") != cfg.today():
            alert(cfg, "Publishing stopped", "Unresolved entries in brain/takedowns.md:\n- " + "\n- ".join(open_items)
                  + "\nResolve them (tick the box) to resume uploads.")
            st["takedown_alerted"] = cfg.today()
            _save_state(cfg, st)
    return open_items


def underperformance(cfg: Config) -> bool:
    """Three consecutive published videos under 50% of channel median (24h views) -> alert."""
    p = cfg.paths.scoreboard
    if not p.exists():
        return False
    with p.open() as f:
        rows = [r for r in csv.DictReader(f) if r.get("checkpoint") == "24h" and r.get("views")]
    if len(rows) < 6:
        return False
    rows.sort(key=lambda r: r.get("published_at", ""))
    views = [float(r["views"]) for r in rows]
    median = statistics.median(views)
    ratio = cfg.get("alerts", {}).get("underperform_ratio", 0.5)
    run = cfg.get("alerts", {}).get("underperform_run", 3)
    last = rows[-run:]
    if all(float(r["views"]) < ratio * median for r in last):
        key = ",".join(r["youtube_id"] for r in last)
        st = _state(cfg)
        if st.get("underperf_alerted") != key:
            alert(cfg, "Underperformance", f"The last {run} videos ({key}) each got under {ratio:.0%} of the "
                                           f"channel median of {median:.0f} views at 24h.")
            st["underperf_alerted"] = key
            _save_state(cfg, st)
        return True
    return False


def sweep(cfg: Config, q: Queue) -> dict:
    perms = Permissions.load(cfg.paths.permissions)
    alert_removed_creators(cfg, perms)
    return {
        "rejected_unapproved": reject_unapproved(cfg, q, perms),
        "rejected_bounds": reject_bad_bounds(cfg, q),
        "takedowns_open": len(takedown_block(cfg)),
        "underperforming": underperformance(cfg),
    }
