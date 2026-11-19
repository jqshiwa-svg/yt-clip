"""Project paths, config.yaml and .env loading."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(os.environ.get("CLIPPER_ROOT", Path(__file__).resolve().parent.parent))


@dataclass
class Paths:
    root: Path

    @property
    def brain(self) -> Path: return self.root / "brain"
    @property
    def media(self) -> Path: return self.root / "media"
    @property
    def raw(self) -> Path: return self.media / "raw"
    @property
    def transcripts(self) -> Path: return self.media / "transcripts"
    @property
    def edits(self) -> Path: return self.media / "edits"
    @property
    def comments(self) -> Path: return self.media / "comments"
    @property
    def tts(self) -> Path: return self.media / "tts"
    @property
    def review(self) -> Path: return self.media / "review"
    @property
    def reports(self) -> Path: return self.root / "reports"
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def db(self) -> Path: return self.root / "queue.db"
    @property
    def permissions(self) -> Path: return self.brain / "permissions.md"
    @property
    def takedowns(self) -> Path: return self.brain / "takedowns.md"
    @property
    def intake_csv(self) -> Path: return self.brain / "intake.csv"
    @property
    def scoreboard(self) -> Path: return self.brain / "scoreboard.csv"

    def ensure(self) -> None:
        for p in (self.brain, self.raw, self.transcripts, self.edits, self.comments,
                  self.tts, self.review, self.reports, self.logs):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Config:
    data: dict[str, Any]
    paths: Paths
    env: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.data.get("timezone", "UTC"))

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().date().isoformat()

    def secret(self, name: str) -> str:
        """Secrets come only from the environment / .env, never from config.yaml."""
        return self.env.get(name) or os.environ.get(name, "")


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load(root: Path | None = None) -> Config:
    root = Path(root or ROOT)
    data = yaml.safe_load((root / "config.yaml").read_text()) or {}
    env = _read_dotenv(root / ".env")
    for k, v in env.items():
        os.environ.setdefault(k, v)
    paths = Paths(root)
    paths.ensure()
    return Config(data=data, paths=paths, env=env)
