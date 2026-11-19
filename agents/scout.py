"""Scout: research, permissions, intake URLs. Web search only for this agent."""
from __future__ import annotations

from typing import Any

from clipper.config import Config
from clipper.logs import alert, log
from clipper.permissions import Permissions, render_table
from scripts.intake import read_csv, write_csv

from .base import Agent, Feedback, S, Tool, obj


class Scout(Agent):
    name = "scout"
    web = True
    reads = ["brain/watchlist.md", "brain/permissions.md", "brain/research_brain.md", "brain/outreach.md",
             "brain/facts.md", "brain/policy_notes.md", "brain/viral_db.csv", "brain/market_brief.md", "brain/intake.csv"]
    writes = ["brain/permissions.md", "brain/outreach.md", "brain/facts.md", "brain/policy_notes.md",
              "brain/viral_db.csv", "brain/market_brief.md", "brain/research_brain.md"]

    def __init__(self, cfg: Config, client: Any = None):
        super().__init__(cfg, client)
        self.before = Permissions.load(cfg.paths.permissions)

    def t_add_intake(self, url: str, creator: str, date: str) -> str:
        perms = Permissions.load(self.cfg.paths.permissions)
        if not perms.is_approved(creator):
            raise Feedback(f"'{creator}' is not approved in brain/permissions.md; not added")
        if not url.startswith(("https://", "http://")):
            raise Feedback("url must be an http(s) link")
        rows = read_csv(self.cfg.paths.intake_csv)
        if any(r.get("url") == url for r in rows):
            return "already in intake.csv"
        rows.append({"url": url, "creator": perms.get(creator).creator, "date": date, "status": "new", "note": ""})
        write_csv(self.cfg.paths.intake_csv, rows)
        return "added"

    def t_escalate(self, kind: str, message: str) -> str:
        alert(self.cfg, f"Scout: {kind}", message)
        return "owner alerted"

    def tools(self) -> list[Tool]:
        return self.file_tools() + [
            Tool("add_intake", "Queue a GTA 6 stream/video URL from an APPROVED creator for download.",
                 obj({"url": S, "creator": S, "date": S}), self.t_add_intake),
            Tool("escalate", "Alert the owner (creator objection, permission withdrawn, risky policy change).",
                 obj({"kind": S, "message": S}), self.t_escalate),
        ]

    def first_message(self) -> str:
        now = self.cfg.now()
        weekly = now.weekday() == 6 or not (self.cfg.paths.brain / "market_brief.md").exists()
        approved = ", ".join(c.creator for c in self.before.approved()) or "none yet"
        return (f"Run time: {now.strftime('%A %Y-%m-%d %H:%M')} ({self.cfg.get('timezone')}).\n"
                f"Approved creators right now: {approved}.\n"
                f"Weekly tasks (re-check proof links, rewrite market_brief.md) are {'DUE' if weekly else 'not due'} this run.\n"
                "Start by reading brain/permissions.md, brain/watchlist.md and brain/research_brain.md, then work "
                "through your steps. Finish with the `finish` tool.")

    def enforce_permissions(self) -> list[str]:
        """After the run: undo approvals the Scout isn't allowed to make."""
        after = Permissions.load(self.cfg.paths.permissions)
        today = self.cfg.today()
        before = {c.key: c for c in self.before.creators}
        changed = []
        for c in after.creators:
            old = before.get(c.key)
            if old and old.status == "removed" and c.status != "removed":
                c.status = "removed"
                changed.append(f"{c.creator}: removed creators can only be restored by the owner")
            newly = c.status == "approved" and (not old or not old.is_approved())
            if newly and (c.date_checked.strip() != today or c.missing_evidence()):
                c.status = "unclear"
                changed.append(f"{c.creator}: approval lacked today's date, proof link or wording -> unclear")
        if changed:
            head = "# Permissions\n\nCreators we may clip. Maintained by the Scout; see agents/prompts/scout.md for the format.\n\n"
            self.cfg.paths.permissions.write_text(head + render_table(after.creators) + "\n", encoding="utf-8")
            log(self.cfg, "scout", "permissions_reverted", changes=changed)
        return changed

    def run(self) -> dict[str, Any]:
        res = super().run()
        res["permission_fixes"] = self.enforce_permissions()
        return res


def run(cfg: Config, client: Any = None) -> dict[str, Any]:
    return Scout(cfg, client).run()
