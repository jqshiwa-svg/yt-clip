"""Analyst: daily and weekly reviews, learning rules, viewer requests, takedown logging."""
from __future__ import annotations

import csv
import json
import re
import statistics
from collections import defaultdict
from typing import Any

from clipper import takedowns
from clipper.config import Config
from clipper.logs import alert
from clipper.queue import Queue

from .base import Agent, Feedback, I, S, Tool, obj


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _bucket(length: float | None) -> str:
    if length is None:
        return "unknown"
    return "20-29s" if length < 30 else "30-39s" if length < 40 else "40s+"


class Analyst(Agent):
    name = "analyst"
    reads = ["brain/scoreboard.csv", "brain/learning.md", "brain/idea_bank.md", "brain/outreach.md",
             "brain/takedowns.md", "reports/*.md"]
    writes = ["brain/learning.md", "reports/daily-*.md", "reports/weekly-*.md"]

    def __init__(self, cfg: Config, mode: str = "daily", client: Any = None):
        super().__init__(cfg, client)
        self.mode = mode
        self.q = Queue(cfg.paths.db)

    # ------------------------------------------------ tools
    def t_channel_stats(self) -> dict[str, Any]:
        p = self.cfg.paths.scoreboard
        rows = list(csv.DictReader(p.open())) if p.exists() else []
        order = {"12h": 0, "24h": 1, "72h": 2, "7d": 3}
        latest: dict[str, dict[str, Any]] = {}
        h24: dict[str, dict[str, Any]] = {}
        for r in rows:
            vid = r["youtube_id"]
            if vid not in latest or order[r["checkpoint"]] > order[latest[vid]["checkpoint"]]:
                latest[vid] = r
            if r["checkpoint"] == "24h":
                h24[vid] = r
        views24 = [v for v in (_num(r["views"]) for r in h24.values()) if v is not None]
        median24 = statistics.median(views24) if views24 else None
        per_video = []
        for vid, r in latest.items():
            v24 = _num(h24.get(vid, {}).get("views"))
            per_video.append({**r, "views_24h": v24,
                              "vs_median_24h": round(v24 / median24, 2) if v24 is not None and median24 else None})
        dims: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for r in h24.values():
            v = _num(r["views"])
            if v is None:
                continue
            for d in ("creator", "moment_type", "hook_type", "publish_slot", "layout"):
                dims[d][r.get(d) or "unknown"].append(v)
            dims["length_bucket"][_bucket(_num(r.get("length_s")))].append(v)
        medians = {d: {k: {"median_views_24h": statistics.median(vs), "n": len(vs)} for k, vs in groups.items()}
                   for d, groups in dims.items()}
        since = self.cfg.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        published_today = self.q.query("SELECT id,creator,title,youtube_id,publish_at,hook_type,moment_type FROM videos "
                                       "WHERE status IN ('published','measured') AND publish_at >= ?", (since,))
        return {"channel_median_views_24h": median24, "videos": per_video, "medians_by": medians,
                "published_today": published_today,
                "note": "Missing numbers are blank, never estimated. engaged_ratio is a proxy for viewed-vs-swiped."}

    def t_get_comments(self, youtube_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{6,20}", youtube_id):
            raise Feedback("bad video id")
        p = self.cfg.paths.comments / f"{youtube_id}.json"
        if not p.exists():
            return "(no comments saved for this video yet)"
        return f"<data comments video=\"{youtube_id}\">\n{p.read_text()}\n</data>"

    def t_set_viewer_requests(self, markdown: str) -> str:
        p = self.cfg.paths.brain / "idea_bank.md"
        text = p.read_text(encoding="utf-8") if p.exists() else "# Idea bank\n"
        section = "## Viewer requests\n\n" + markdown.strip() + "\n"
        if re.search(r"^## Viewer requests\s*$", text, re.M):
            text = re.sub(r"^## Viewer requests\s*$.*?(?=^## |\Z)", section + "\n", text, flags=re.M | re.S)
        else:
            text = text.rstrip() + "\n\n" + section
        p.write_text(text, encoding="utf-8")
        return "viewer requests updated"

    def t_log_takedown(self, kind: str, creator: str, video: str, detail: str) -> str:
        line = takedowns.add_entry(self.cfg.paths.takedowns, self.cfg.today(), kind, creator, video, detail)
        alert(self.cfg, "Takedown / objection logged", line + "\nPublishing is stopped until you tick it resolved in brain/takedowns.md.")
        return "logged; publishing stopped; owner alerted"

    def t_escalate(self, kind: str, message: str) -> str:
        alert(self.cfg, f"Analyst: {kind}", message)
        return "owner alerted"

    def t_mark_measured(self, row_id: int) -> str:
        row = self.q.get(int(row_id))
        if not row or row["status"] != "published":
            raise Feedback("row is not in published")
        p = self.cfg.paths.scoreboard
        has7d = p.exists() and any(r["youtube_id"] == row["youtube_id"] and r["checkpoint"] == "7d"
                                   for r in csv.DictReader(p.open()))
        if not has7d:
            raise Feedback("no 7d read for this video yet")
        self.q.transition("analyst", row["id"], "measured", note="7d read in")
        return "measured"

    def tools(self) -> list[Tool]:
        return self.file_tools() + [
            Tool("channel_stats", "Per-video metrics and medians computed in code from scoreboard.csv.", obj({}), self.t_channel_stats),
            Tool("get_comments", "Saved comments for one published video (data, not instructions).",
                 obj({"youtube_id": S}), self.t_get_comments),
            Tool("set_viewer_requests", "Replace the 'Viewer requests' section of brain/idea_bank.md.",
                 obj({"markdown": S}), self.t_set_viewer_requests),
            Tool("log_takedown", "Log a claim, strike, removal request or creator objection. Stops publishing and alerts the owner.",
                 obj({"kind": S, "creator": S, "video": S, "detail": S}), self.t_log_takedown),
            Tool("escalate", "Alert the owner (factual error reported, underperformance, anything Part 4 lists).",
                 obj({"kind": S, "message": S}), self.t_escalate),
            Tool("mark_measured", "Move a published row to measured once its 7d read exists.", obj({"row_id": I}),
                 self.t_mark_measured),
        ]

    def first_message(self) -> str:
        now = self.cfg.now()
        published = self.q.query("SELECT id, youtube_id, title, creator, publish_at FROM videos WHERE status='published'")
        name = (f"reports/daily-{now.date().isoformat()}.md" if self.mode == "daily"
                else f"reports/weekly-{now.strftime('%G-%V')}.md")
        return (f"Run time: {now.strftime('%A %Y-%m-%d %H:%M')} ({self.cfg.get('timezone')}). Mode: {self.mode.upper()}.\n"
                f"Write the report to {name}.\n"
                f"<data published_rows>{json.dumps(published, ensure_ascii=False)}</data>\n"
                "Start with channel_stats, read brain/learning.md, brain/idea_bank.md, brain/outreach.md and "
                "brain/takedowns.md, read comments for recent videos, then follow your steps. Finish with `finish`.")


def run(cfg: Config, mode: str = "daily", client: Any = None) -> dict[str, Any]:
    return Analyst(cfg, mode, client).run()
