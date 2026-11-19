"""Writer: finds moments in transcripts and writes scripted queue rows."""
from __future__ import annotations

import json
import re
from datetime import timezone
from pathlib import Path
from typing import Any

from clipper.config import Config
from clipper.media import list_sources, read_sidecar
from clipper.permissions import Permissions
from clipper.queue import Queue
from clipper.safety import source_duration

from .base import Agent, B, Feedback, N, S, Tool, obj, I

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
SCRIPT_FIELDS = {
    "creator": S, "source_file": S, "clip_start": N, "clip_end": N, "credit_line": S, "moment": S,
    "moment_type": S, "hook": S, "hook_type": S, "structure": S, "script": S, "title": S, "description": S,
}
MAX_ATTEMPTS = 3


class Writer(Agent):
    name = "writer"
    reads = ["brain/permissions.md", "brain/market_brief.md", "brain/viral_db.csv", "brain/facts.md",
             "brain/research_brain.md", "brain/learning.md", "brain/idea_bank.md"]
    writes = ["brain/idea_bank.md"]

    def __init__(self, cfg: Config, client: Any = None):
        super().__init__(cfg, client)
        self.q = Queue(cfg.paths.db)

    # ------------------------------------------------ helpers
    def _today_start_utc(self) -> str:
        return self.cfg.now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()

    def _scripted_today(self) -> list[dict[str, Any]]:
        return self.q.query(
            "SELECT v.* FROM videos v JOIN transitions t ON t.video_id=v.id "
            "WHERE t.actor='writer' AND t.from_status='idea' AND t.to_status='scripted' AND t.at>=? "
            "GROUP BY v.id", (self._today_start_utc(),))

    def _last_script(self, exclude: int | None = None) -> dict[str, Any] | None:
        rows = self.q.query(
            "SELECT * FROM videos WHERE status IN ('scripted','edited','approved','published','measured') "
            "AND id != ? ORDER BY updated_at DESC, id DESC LIMIT 1", (exclude or -1,))
        return rows[0] if rows else None

    def _validate(self, f: dict[str, Any], row_id: int | None = None) -> list[str]:
        errs: list[str] = []
        perms = Permissions.load(self.cfg.paths.permissions)
        c = perms.get(f.get("creator", ""))
        if not c or not c.is_approved():
            return [f"creator '{f.get('creator')}' is not approved in permissions.md; drop this moment"]
        credit = (f.get("credit_line") or "").strip()
        if not credit:
            errs.append("credit_line is required (use the creator's credit format from permissions.md)")
        src = self.cfg.paths.raw / Path(f.get("source_file") or "").name
        if not src.is_file():
            errs.append(f"source_file {f.get('source_file')} not found; use list_sources")
        else:
            owner = read_sidecar(src).get("creator", "")
            if perms.get(owner) is not c:
                errs.append(f"source_file belongs to '{owner}', not '{f.get('creator')}'")
            dur = source_duration(self.cfg, src.name)
            a, b = float(f.get("clip_start", -1)), float(f.get("clip_end", -1))
            if dur is None or a < 0 or b <= a or b > dur:
                errs.append(f"clip window {a}-{b} must be inside the source (0-{dur})")
            elif not 20 <= b - a <= 90:
                errs.append("clip window must be 20 to 90 seconds")
        title = (f.get("title") or "").strip()
        if not title or len(title) >= 60:
            errs.append("title must be under 60 characters")
        if title and title.upper() == title and any(ch.isalpha() for ch in title):
            errs.append("no all-caps titles")
        desc = (f.get("description") or "").strip()
        if credit and credit not in desc:
            errs.append("description must include the credit_line")
        for field in ("title", "description", "hook", "script"):
            if re.search(r"subscribe|like and sub", f.get(field) or "", re.I):
                errs.append(f"no 'like and subscribe' anywhere ({field})")
        allowed_links = set(URL_RE.findall(c.conditions))
        for link in URL_RE.findall(desc):
            if link not in allowed_links:
                errs.append(f"no links in the description unless permissions.md requires them ({link})")
        prev = self._last_script(exclude=row_id)
        if prev:
            if prev.get("hook_type") and prev["hook_type"] == f.get("hook_type"):
                errs.append(f"hook_type '{f.get('hook_type')}' matches the previous script; use a different one")
            if prev.get("structure") and prev["structure"] == f.get("structure"):
                errs.append(f"structure '{f.get('structure')}' matches the previous script; use a different one")
        return errs

    def _caps(self, source_file: str) -> list[str]:
        today = self._scripted_today()
        errs = []
        if len(today) >= int(self.cfg.get("daily_target", 3)):
            errs.append(f"daily target of {self.cfg.get('daily_target', 3)} scripted rows reached; stop scripting")
        if sum(1 for r in today if r["source_file"] == Path(source_file).name) >= 2:
            errs.append("already two Shorts from this stream today")
        return errs

    # ------------------------------------------------ tools
    def t_list_sources(self) -> list[dict[str, Any]]:
        out = []
        today = self._scripted_today()
        for s in list_sources(self.cfg.paths.raw):
            tp = self.cfg.paths.transcripts / (Path(s["source_file"]).stem + ".json")
            s["transcribed"] = tp.exists()
            s["shorts_today"] = sum(1 for r in today if r["source_file"] == s["source_file"])
            s["shorts_total"] = len(self.q.query("SELECT id FROM videos WHERE source_file=?", (s["source_file"],)))
            out.append(s)
        return out

    def t_get_transcript(self, source_file: str, start: float = 0, end: float | None = None, words: bool = False) -> str:
        tp = self.cfg.paths.transcripts / (Path(source_file).stem + ".json")
        if not tp.exists():
            raise Feedback("no transcript yet for this source")
        data = json.loads(tp.read_text())
        lines = []
        for s in data["segments"]:
            if s["end"] < start or (end is not None and s["start"] > end):
                continue
            if words:
                lines.append(" ".join(f"{w['word']}@{w['start']:.1f}" for w in s.get("words", [])))
            else:
                lines.append(f"[{s['start']:.1f}] {s['text']}")
        text = "\n".join(lines)
        if len(text) > 180_000:
            text = text[:180_000] + "\n[truncated: pass a narrower start/end window]"
        return f"<data transcript=\"{source_file}\" language=\"{data.get('language')}\">\n{text}\n</data>"

    def t_recent_rows(self, limit: int = 15) -> list[dict[str, Any]]:
        return self.q.query(
            "SELECT id,status,creator,title,hook,hook_type,structure,moment_type,source_file,updated_at "
            "FROM videos ORDER BY updated_at DESC, id DESC LIMIT ?", (min(int(limit), 50),))

    def t_list_returned_rows(self) -> list[dict[str, Any]]:
        return self.q.query(
            "SELECT id,status,creator,source_file,clip_start,clip_end,moment,hook,hook_type,structure,script,title,"
            "description,credit_line,review_notes,attempts FROM videos "
            "WHERE (status='idea' AND review_notes IS NOT NULL) "
            "OR (status='rejected' AND rejected_by='reviewer' AND attempts < ?) ORDER BY id", (MAX_ATTEMPTS,))

    def t_create_script(self, **f: Any) -> str:
        errs = self._validate(f) + self._caps(f.get("source_file", ""))
        if errs:
            raise Feedback("; ".join(errs))
        meta = read_sidecar(self.cfg.paths.raw / Path(f["source_file"]).name)
        perms = Permissions.load(self.cfg.paths.permissions)
        f["creator"] = perms.get(f["creator"]).creator
        f["source_file"] = Path(f["source_file"]).name
        vid = self.q.create("writer", source_url=meta.get("source_url", ""), **f)
        self.q.transition("writer", vid, "scripted", note="scripted")
        return f"row {vid} scripted"

    def t_revise_script(self, row_id: int, **f: Any) -> str:
        row = self.q.get(int(row_id))
        if not row:
            raise Feedback("no such row")
        if row["status"] == "rejected" and row.get("rejected_by") != "reviewer":
            raise Feedback("only Reviewer rejections can be revised")
        if int(row.get("attempts") or 0) >= MAX_ATTEMPTS:
            raise Feedback("this row has been revised too many times; leave it")
        merged = {k: row.get(k) for k in SCRIPT_FIELDS} | {k: v for k, v in f.items() if v is not None}
        errs = self._validate(merged, row_id=row["id"]) + self._caps(merged.get("source_file", ""))
        if errs:
            raise Feedback("; ".join(errs))
        if row["status"] == "rejected":
            self.q.transition("writer", row["id"], "idea", note="revising after review")
        self.q.update_fields("writer", row["id"], "idea", **{k: merged[k] for k in SCRIPT_FIELDS},
                             edit_spec=None, output_file=None, attempts=int(row.get("attempts") or 0) + 1)
        self.q.transition("writer", row["id"], "scripted", note="revised", review_notes=None)
        return f"row {row['id']} revised and scripted"

    def tools(self) -> list[Tool]:
        script_schema = obj(dict(SCRIPT_FIELDS))
        revise_props = {"row_id": I, **SCRIPT_FIELDS}
        return self.file_tools() + [
            Tool("list_sources", "List downloaded source files with creator, duration, transcript status and Shorts taken.",
                 obj({}), self.t_list_sources),
            Tool("get_transcript", "Read a source's transcript, optionally a time window, optionally with word timings.",
                 obj({"source_file": S, "start": N, "end": N, "words": B}, ["source_file"]), self.t_get_transcript),
            Tool("recent_rows", "The most recent queue rows (titles, hook types, structures).",
                 obj({"limit": I}, []), self.t_recent_rows),
            Tool("list_returned_rows", "Rows sent back by the Editor or Reviewer, with their notes.",
                 obj({}), self.t_list_returned_rows),
            Tool("create_script", "Create a queue row for one moment and move it to scripted. Validated in code.",
                 script_schema, self.t_create_script),
            Tool("revise_script", "Fix a returned row (pass only fields you change) and move it back to scripted.",
                 obj(revise_props, ["row_id"]), self.t_revise_script),
        ]

    def first_message(self) -> str:
        now = self.cfg.now()
        done = len(self._scripted_today())
        return (f"Run time: {now.strftime('%A %Y-%m-%d %H:%M')} ({self.cfg.get('timezone')}). "
                f"Scripted today so far: {done} of {self.cfg.get('daily_target', 3)}.\n"
                "Read brain/learning.md, brain/market_brief.md (note the Saturated list), brain/permissions.md and "
                "brain/idea_bank.md first. Then list_returned_rows, list_sources, and work through your steps. "
                "Finish with the `finish` tool.")


def run(cfg: Config, client: Any = None) -> dict[str, Any]:
    return Writer(cfg, client).run()
