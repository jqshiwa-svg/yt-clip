"""Parse brain/permissions.md, the single source of truth for whose footage we may use.

Format (one Markdown table; the Scout keeps it current):

| creator | status | channels | proof_link | wording | conditions | date_checked |
|---|---|---|---|---|---|---|
| ExampleStreamer | approved | https://www.youtube.com/@example; https://www.twitch.tv/example | https://... | "Anyone can clip..." | credit @example on screen; no music | 2026-11-19 |

status is one of: approved, unclear, paused, removed, requested.
channels is a ';'-separated list of channel URLs or YouTube channel ids (UC...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

STATUSES = {"approved", "unclear", "paused", "removed", "requested"}
COLUMNS = ["creator", "status", "channels", "proof_link", "wording", "conditions", "date_checked"]


@dataclass
class Creator:
    creator: str
    status: str
    channels: list[str] = field(default_factory=list)
    proof_link: str = ""
    wording: str = ""
    conditions: str = ""
    date_checked: str = ""

    @property
    def key(self) -> str:
        return norm_name(self.creator)

    def is_approved(self) -> bool:
        """Approved only with the evidence the brief requires: proof link, exact wording, a date."""
        return (
            self.status == "approved"
            and bool(self.proof_link.strip())
            and bool(self.wording.strip())
            and _valid_date(self.date_checked)
        )

    def missing_evidence(self) -> list[str]:
        missing = []
        if not self.proof_link.strip():
            missing.append("proof_link")
        if not self.wording.strip():
            missing.append("wording")
        if not _valid_date(self.date_checked):
            missing.append("date_checked")
        return missing


def _valid_date(s: str) -> bool:
    try:
        date.fromisoformat(s.strip())
        return True
    except ValueError:
        return False


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def norm_channel(ref: str) -> str:
    """Normalise a channel URL / id so different spellings compare equal."""
    ref = ref.strip()
    if not ref:
        return ""
    if re.fullmatch(r"UC[A-Za-z0-9_\-]{22}", ref):
        return f"youtube:{ref}"
    if "://" not in ref:
        ref = "https://" + ref
    u = urlparse(ref)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = u.path.rstrip("/")
    if host in ("youtube.com", "youtu.be"):
        m = re.match(r"^/channel/(UC[A-Za-z0-9_\-]{22})", path)
        if m:
            return f"youtube:{m.group(1)}"
        m = re.match(r"^/(@[^/]+)", path)
        if m:
            return f"youtube:{m.group(1).lower()}"
        m = re.match(r"^/(?:c|user)/([^/]+)", path)
        if m:
            return f"youtube:/c/{m.group(1).lower()}"
    if host == "twitch.tv":
        first = path.strip("/").split("/")[0].lower()
        return f"twitch:{first}"
    if host == "kick.com":
        first = path.strip("/").split("/")[0].lower()
        return f"kick:{first}"
    return f"{host}{path.lower()}"


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    # allow escaped pipes inside cells
    parts = re.split(r"(?<!\\)\|", line)
    return [p.strip().replace("\\|", "|") for p in parts]


def parse(text: str) -> list[Creator]:
    creators: list[Creator] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            header = None if not line.strip() else header
            continue
        cells = _split_row(line)
        if header is None:
            lowered = [c.lower() for c in cells]
            if "creator" in lowered and "status" in lowered:
                header = lowered
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        row = dict(zip(header, cells))
        name = row.get("creator", "").strip()
        if not name:
            continue
        status = row.get("status", "").strip().lower()
        if status not in STATUSES:
            status = "unclear"
        channels = [c.strip() for c in re.split(r"[;,\s]+", row.get("channels", "")) if c.strip()]
        creators.append(Creator(
            creator=name, status=status, channels=channels,
            proof_link=row.get("proof_link", ""), wording=row.get("wording", ""),
            conditions=row.get("conditions", ""), date_checked=row.get("date_checked", ""),
        ))
    return creators


class Permissions:
    def __init__(self, creators: list[Creator]):
        self.creators = creators
        self._by_name = {c.key: c for c in creators}

    @classmethod
    def load(cls, path: Path) -> "Permissions":
        return cls(parse(path.read_text(encoding="utf-8")) if path.exists() else [])

    def get(self, name: str) -> Creator | None:
        return self._by_name.get(norm_name(name or ""))

    def is_approved(self, name: str) -> bool:
        c = self.get(name)
        return bool(c and c.is_approved())

    def approved(self) -> list[Creator]:
        return [c for c in self.creators if c.is_approved()]

    def creator_for_channel(self, refs: list[str]) -> Creator | None:
        """Return the approved creator that owns any of these channel refs, else None."""
        wanted = {norm_channel(r) for r in refs if r}
        wanted.discard("")
        for c in self.approved():
            if wanted & {norm_channel(ch) for ch in c.channels}:
                return c
        return None

    def statuses(self) -> dict[str, str]:
        return {c.key: c.status for c in self.creators}


def render_table(creators: list[Creator]) -> str:
    def esc(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for c in creators:
        lines.append("| " + " | ".join(esc(x) for x in [
            c.creator, c.status, "; ".join(c.channels), c.proof_link, c.wording, c.conditions, c.date_checked,
        ]) + " |")
    return "\n".join(lines)
