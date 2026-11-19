"""brain/takedowns.md: claims, strikes and creator objections.

Each entry is a checklist line. Unchecked = unresolved, and blocks all uploads.

- [ ] 2026-11-20 | claim | creator: Example | video: abc123 | what happened
- [x] 2026-11-18 | objection | creator: Other | resolved by owner: video removed
"""
from __future__ import annotations

import re
from pathlib import Path

_OPEN = re.compile(r"^\s*[-*]\s*\[\s\]\s*(.+)$")


def unresolved(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [m.group(1).strip() for line in path.read_text(encoding="utf-8").splitlines()
            if (m := _OPEN.match(line))]


def add_entry(path: Path, date: str, kind: str, creator: str, video: str, detail: str) -> str:
    line = f"- [ ] {date} | {kind} | creator: {creator or '-'} | video: {video or '-'} | {detail.strip()}"
    text = path.read_text(encoding="utf-8") if path.exists() else "# Takedowns, claims and objections\n\n"
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + line + "\n", encoding="utf-8")
    return line
