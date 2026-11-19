"""Editor: writes the edit_spec for one scripted row at a time. Never renders."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clipper import spec as spec_mod
from clipper.config import Config
from clipper.queue import Queue
from clipper.safety import source_duration

from .base import Agent, Feedback, I, S, Tool, obj


def previous_layout(q: Queue, exclude: int) -> str | None:
    rows = q.query("SELECT edit_spec FROM videos WHERE edit_spec IS NOT NULL AND id != ? "
                   "AND status IN ('scripted','edited','approved','published','measured') "
                   "ORDER BY updated_at DESC, id DESC LIMIT 1", (exclude,))
    if not rows:
        return None
    try:
        return json.loads(rows[0]["edit_spec"]).get("layout")
    except (TypeError, ValueError):
        return None


class Editor(Agent):
    name = "editor"
    reads = ["brain/learning.md"]
    writes: list[str] = []

    def __init__(self, cfg: Config, row: dict[str, Any], client: Any = None):
        super().__init__(cfg, client)
        self.q = Queue(cfg.paths.db)
        self.row = row
        self.prev_layout = previous_layout(self.q, row["id"])
        r = cfg["render"]
        self.sfx_dir = cfg.paths.root / r.get("sfx_dir", "assets/sfx")

    def t_submit_edit_spec(self, row_id: int, spec: dict[str, Any] | str) -> str:
        if int(row_id) != self.row["id"]:
            raise Feedback(f"you are editing row {self.row['id']} only")
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except ValueError as e:
                raise Feedback(f"spec is not valid JSON: {e}")
        r = self.cfg["render"]
        errs = spec_mod.validate(
            spec, row=self.row, source_duration=source_duration(self.cfg, self.row["source_file"]),
            layouts=list(self.cfg["layouts"].keys()), previous_layout=self.prev_layout, sfx_dir=self.sfx_dir,
            music_dir=self.cfg.paths.root / (self.cfg.get("music", {}) or {}).get("dir", "assets/music"),
            min_s=r.get("min_seconds", 20), max_s=r.get("max_seconds", 45))
        if errs:
            raise Feedback("; ".join(errs))
        self.q.update_fields("editor", self.row["id"], "scripted", edit_spec=spec)
        self.finished = True
        return "edit_spec saved"

    def t_reject_footage(self, row_id: int, reason: str) -> str:
        if int(row_id) != self.row["id"]:
            raise Feedback(f"you are editing row {self.row['id']} only")
        self.q.transition("editor", self.row["id"], "idea", note=reason, edit_spec=None,
                          review_notes=f"[editor] {reason}")
        self.finished = True
        return "row returned to idea"

    def tools(self) -> list[Tool]:
        return self.file_tools() + [
            Tool("submit_edit_spec", "Save the edit_spec JSON for this row. Validated in code; fix and resubmit on error.",
                 obj({"row_id": I, "spec": {"type": "object"}}), self.t_submit_edit_spec),
            Tool("reject_footage", "The footage cannot support the script: return the row to idea with a reason.",
                 obj({"row_id": I, "reason": S}), self.t_reject_footage),
        ]

    def _words(self) -> str:
        tp = self.cfg.paths.transcripts / (Path(self.row["source_file"]).stem + ".json")
        if not tp.exists():
            return "(no transcript)"
        a, b = float(self.row["clip_start"]), float(self.row["clip_end"])
        out = []
        for s in json.loads(tp.read_text())["segments"]:
            for w in s.get("words", []):
                if a <= w["start"] <= b:
                    out.append(f"{w['start']:.2f}-{w['end']:.2f} {w['word']}")
        return "\n".join(out) or "(no speech in this window)"

    def first_message(self) -> str:
        r = self.row
        fields = {k: r.get(k) for k in ("id", "creator", "credit_line", "source_file", "clip_start", "clip_end",
                                       "moment", "hook", "script", "title", "hook_type", "structure", "moment_type")}
        sfx = sorted(p.name for p in self.sfx_dir.glob("*") if p.is_file()) if self.sfx_dir.exists() else []
        mdir = self.cfg.paths.root / (self.cfg.get("music", {}) or {}).get("dir", "assets/music")
        music = sorted(p.name for p in mdir.glob("*") if p.is_file() and p.suffix != ".md") if mdir.exists() else []
        layouts = "\n".join(f"- {k}: {v}" for k, v in self.cfg["layouts"].items())
        learning = self.t_read_file("brain/learning.md")
        return (f"Write the edit_spec for row {r['id']}.\n\n<data row>\n{json.dumps(fields, indent=1)}\n</data>\n\n"
                f"Layouts:\n{layouts}\nPrevious video's layout: {self.prev_layout or 'none'} (do not repeat it).\n"
                f"Approved sfx library: {sfx or 'empty (use no sfx)'}\n"
                f"Licensed music library: {music or 'empty (no music)'}\n\n{learning}\n\n"
                f"Word timings (SOURCE seconds) for the clip window:\n<data transcript>\n{self._words()}\n</data>\n\n"
                "Submit with submit_edit_spec, or reject_footage if the footage can't support the script.")


def run(cfg: Config, client: Any = None) -> list[dict[str, Any]]:
    q = Queue(cfg.paths.db)
    out = []
    for row in q.by_status("scripted"):
        if row.get("edit_spec"):
            continue
        out.append({"row": row["id"], **Editor(cfg, row, client).run()})
    return out
