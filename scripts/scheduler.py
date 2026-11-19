"""scheduler.py: the long-running process that starts every script and agent on its cadence.

  Scout every 6 hours, then intake
  transcribe every 5 minutes (continuous watcher)
  Writer daily at 07:00, and after each intake for the first 14 days after launch
  Editor, render and Reviewer every 5 minutes when a row is waiting
  upload at each posting time in config.yaml
  metrics hourly (it decides which 12h/24h/72h/7d reads are due)
  Analyst at 21:00 daily, and the weekly review Sunday 21:00
  safety sweep every 5 minutes (creator status, clip bounds, takedowns, underperformance)

Usage: python -m scripts.scheduler           (run forever; Ctrl+C to stop)
       python -m scripts.scheduler --once    (one pass of every job, for the first supervised run)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from clipper import config as config_mod
from clipper import safety
from clipper.config import Config
from clipper.logs import alert, log
from clipper.queue import Queue


def _state_path(cfg: Config):
    return cfg.paths.logs / "scheduler_state.json"


def _load(cfg: Config) -> dict[str, Any]:
    p = _state_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {}


def _save(cfg: Config, st: dict[str, Any]) -> None:
    _state_path(cfg).write_text(json.dumps(st, indent=2))


def guarded(cfg: Config, name: str, fn: Callable[[], Any]) -> Any:
    """Run a job; log it; alert if it has been failing for longer than the outage threshold."""
    st = _load(cfg)
    js = st.setdefault(name, {})
    now = datetime.now(timezone.utc)
    try:
        res = fn()
        js["last_ok"] = now.isoformat()
        js.pop("failing_since", None)
        js.pop("outage_alerted", None)
        log(cfg, "scheduler", "job_ok", job=name, result=res)
        return res
    except Exception as e:
        js.setdefault("failing_since", now.isoformat())
        log(cfg, "scheduler", "job_failed", job=name, error=repr(e), trace=traceback.format_exc()[-1500:])
        hours = float(cfg.get("alerts", {}).get("tool_outage_hours", 6))
        if now - datetime.fromisoformat(js["failing_since"]) > timedelta(hours=hours) and not js.get("outage_alerted"):
            alert(cfg, "Tool outage", f"Job '{name}' has failed for over {hours:.0f} hours. Last error: {e}")
            js["outage_alerted"] = True
        return None
    finally:
        _save(cfg, st)


def within_launch_window(cfg: Config) -> bool:
    launch = date.fromisoformat(str(cfg.get("launch_date", cfg.today())))
    return cfg.now().date() <= launch + timedelta(days=14)


# ------------------------------------------------------------------ jobs
def job_scout_then_intake(cfg: Config) -> None:
    from agents import scout
    from scripts import intake
    guarded(cfg, "scout", lambda: scout.run(cfg))
    counts = guarded(cfg, "intake", lambda: intake.run(cfg))
    if counts and counts.get("downloaded") and within_launch_window(cfg):
        job_transcribe(cfg)  # writer needs transcripts; this is a no-op for files already done
        job_writer(cfg)


def job_transcribe(cfg: Config) -> None:
    from scripts import transcribe
    guarded(cfg, "transcribe", lambda: transcribe.run_once(cfg))


def job_writer(cfg: Config) -> None:
    from agents import writer
    guarded(cfg, "writer", lambda: writer.run(cfg))


def job_production(cfg: Config) -> None:
    """Editor -> render -> Reviewer, only when rows are waiting."""
    from agents import editor, reviewer
    from scripts import render
    q = Queue(cfg.paths.db)
    guarded(cfg, "safety", lambda: safety.sweep(cfg, q))
    if any(not r.get("edit_spec") for r in q.by_status("scripted")):
        guarded(cfg, "editor", lambda: editor.run(cfg))
    if any(r.get("edit_spec") for r in q.by_status("scripted")):
        guarded(cfg, "render", lambda: render.run(cfg))
    if q.by_status("edited"):
        guarded(cfg, "reviewer", lambda: reviewer.run(cfg))
        waiting = [r["id"] for r in q.by_status("approved") if not r.get("owner_ok_at")]
        if waiting and cfg.get("require_owner_ok", True):
            log(cfg, "scheduler", "waiting_owner_ok", rows=waiting)


def job_upload(cfg: Config) -> None:
    from scripts import upload
    q = Queue(cfg.paths.db)
    guarded(cfg, "safety", lambda: safety.sweep(cfg, q))
    guarded(cfg, "upload", lambda: upload.run(cfg))


def job_metrics(cfg: Config) -> None:
    from scripts import metrics
    guarded(cfg, "metrics", lambda: metrics.run(cfg))


def job_analyst(cfg: Config, mode: str) -> None:
    from agents import analyst
    guarded(cfg, f"analyst_{mode}", lambda: analyst.run(cfg, mode))


def run_once(cfg: Config) -> None:
    job_scout_then_intake(cfg)
    job_transcribe(cfg)
    job_writer(cfg)
    job_production(cfg)
    job_upload(cfg)
    job_metrics(cfg)


def main() -> None:
    cfg = config_mod.load()
    if "--once" in sys.argv:
        run_once(cfg)
        return
    from apscheduler.schedulers.blocking import BlockingScheduler
    s = BlockingScheduler(timezone=cfg.tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900})
    s.add_job(job_scout_then_intake, "interval", hours=6, args=[cfg], next_run_time=datetime.now(cfg.tz), id="scout")
    s.add_job(job_transcribe, "interval", minutes=5, args=[cfg], id="transcribe")
    s.add_job(job_writer, "cron", hour=7, minute=0, args=[cfg], id="writer")
    s.add_job(job_production, "interval", minutes=5, args=[cfg], id="production")
    for t in cfg.get("posting_times", []):
        hh, mm = (int(x) for x in t.split(":"))
        s.add_job(job_upload, "cron", hour=hh, minute=mm, args=[cfg], id=f"upload_{hh:02d}{mm:02d}")
    s.add_job(job_metrics, "interval", hours=1, args=[cfg], id="metrics")
    s.add_job(job_analyst, "cron", hour=21, minute=0, args=[cfg, "daily"], id="analyst_daily")
    s.add_job(job_analyst, "cron", day_of_week="sun", hour=21, minute=30, args=[cfg, "weekly"], id="analyst_weekly")
    log(cfg, "scheduler", "started", jobs=[j.id for j in s.get_jobs()])
    print(f"Scheduler running in {cfg.get('timezone')}. Logs in logs/, reports in reports/. Ctrl+C to stop.")
    try:
        s.start()
    except (KeyboardInterrupt, SystemExit):
        log(cfg, "scheduler", "stopped")


if __name__ == "__main__":
    main()
