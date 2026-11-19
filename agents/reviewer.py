"""Reviewer: veto on every edited row. Sees the finished video's transcript and frames, not the Writer's reasoning."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from clipper.config import Config
from clipper.media import probe_duration, run_ffmpeg
from clipper.permissions import Permissions, render_table
from clipper.queue import Queue

from .base import Agent, B, Feedback, I, S, Tool, obj

CHECKS = [f"check_{i}" for i in range(1, 10)]
Transcriber = Callable[[Config, Path, Path], dict[str, Any]]


def default_transcriber(cfg: Config, src: Path, out: Path) -> dict[str, Any]:
    from scripts.transcribe import transcribe_file
    return transcribe_file(cfg, src, out)


class Reviewer(Agent):
    name = "reviewer"
    reads = ["brain/permissions.md", "brain/facts.md"]
    writes: list[str] = []

    def __init__(self, cfg: Config, row: dict[str, Any], client: Any = None,
                 transcriber: Transcriber = default_transcriber):
        super().__init__(cfg, client)
        self.q = Queue(cfg.paths.db)
        self.row = row
        self.video = cfg.paths.edits / (row.get("output_file") or "")
        self.transcriber = transcriber
        self.decision: str | None = None

    def t_submit_review(self, row_id: int, decision: str, anti_slop_sentence: str, notes: str, **checks: bool) -> str:
        if int(row_id) != self.row["id"]:
            raise Feedback(f"you are reviewing row {self.row['id']} only")
        missing = [c for c in CHECKS if c not in checks]
        if missing:
            raise Feedback(f"all nine checks are required; missing {missing}")
        failed = [c for c in CHECKS if not checks[c]]
        # code re-checks, independent of the model
        perms = Permissions.load(self.cfg.paths.permissions)
        c = perms.get(self.row["creator"])
        if not c or not c.is_approved():
            failed.append("code: creator not approved today")
        if not self.row.get("credit_line") or self.row["credit_line"] not in (self.row.get("description") or ""):
            failed.append("code: credit missing from description")
        dur = probe_duration(self.video)
        if not 20 <= dur <= 60:
            failed.append(f"code: duration {dur:.1f}s outside 20-60s")
        if not anti_slop_sentence.strip():
            failed.append("anti-slop sentence missing")
        approve = decision == "approved" and not failed
        status = "approved" if approve else "rejected"
        note = notes.strip()
        if not approve:
            note = f"Failed: {', '.join(failed) or 'reviewer decision'}. {note}"
        note += f"\nAnti-slop: {anti_slop_sentence.strip()}"
        self.q.transition("reviewer", self.row["id"], status, note=note[:500], review_notes=note,
                          rejected_by=None if approve else "reviewer")
        self.decision = status
        self.finished = True
        return f"row {self.row['id']} {status}"

    def tools(self) -> list[Tool]:
        props = {"row_id": I, "decision": {"type": "string", "enum": ["approved", "rejected"]},
                 "anti_slop_sentence": S, "notes": S, **{c: B for c in CHECKS}}
        return self.file_tools() + [
            Tool("submit_review", "Record your verdict with all nine checks (true = passes).", obj(props), self.t_submit_review),
        ]

    # ------------------------------------------------ context
    def _frames(self) -> list[dict[str, Any]]:
        dur = probe_duration(self.video)
        times = sorted({0.5, 2.0, 4.0, 6.5, dur * 0.5, dur * 0.85})
        out = []
        tmp = self.cfg.paths.review / f"{self.row['id']}"
        tmp.mkdir(parents=True, exist_ok=True)
        for t in times:
            f = tmp / f"f_{t:05.1f}.jpg"
            run_ffmpeg(["-ss", f"{t:.2f}", "-i", str(self.video), "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(f)])
            out.append({"type": "text", "text": f"Frame at {t:.1f}s:"})
            out.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(f.read_bytes()).decode()}})
        return out

    def _transcript(self) -> str:
        out = self.cfg.paths.review / f"{self.row['id']}.json"
        data = self.transcriber(self.cfg, self.video, out)
        return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in data.get("segments", [])) or "(no speech)"

    def first_message(self) -> list[dict[str, Any]]:
        r = self.row
        perms = Permissions.load(self.cfg.paths.permissions)
        c = perms.get(r["creator"])
        entry = render_table([c]) if c else f"(no entry for {r['creator']})"
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        recent = self.q.query("SELECT title, hook, moment FROM videos WHERE status IN ('published','measured') "
                              "AND publish_at >= ? ORDER BY publish_at DESC", (since,))
        spec = json.loads(r["edit_spec"]) if r.get("edit_spec") else {}
        text = (
            f"Review row {r['id']}.\n\n"
            f"<data title>{r.get('title')}</data>\n<data description>{r.get('description')}</data>\n"
            f"<data script>{r.get('script')}</data>\n<data hook>{r.get('hook')}</data>\n"
            f"<data edit_spec>{json.dumps(spec, indent=1)}</data>\n"
            f"<data permissions_entry>\n{entry}\n</data>\n"
            f"Finished video duration: {probe_duration(self.video):.1f}s\n"
            f"<data finished_video_transcript>\n{self._transcript()}\n</data>\n"
            f"<data published_last_7_days>{json.dumps(recent, ensure_ascii=False)}</data>\n"
            "brain/facts.md is available through read_file for check 5. Frames from the finished video follow."
        )
        return [{"type": "text", "text": text}, *self._frames()]


def run(cfg: Config, client: Any = None, transcriber: Transcriber = default_transcriber) -> list[dict[str, Any]]:
    q = Queue(cfg.paths.db)
    out = []
    for row in q.by_status("edited"):
        rv = Reviewer(cfg, row, client, transcriber)
        res = rv.run()
        if rv.decision is None and (q.get(row["id"]) or {}).get("status") == "edited":
            # no verdict (refusal, tool failure, ran out of turns): unsure means reject
            q.transition("safety", row["id"], "rejected", note="reviewer gave no verdict",
                         review_notes="[reviewer] no verdict this run; rejected by default", rejected_by="reviewer")
            rv.decision = "rejected"
        out.append({"row": row["id"], "decision": rv.decision, **res})
    return out
