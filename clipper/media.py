"""Small FFmpeg/ffprobe helpers and source-file metadata."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def probe_video(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return {"width": int(s["width"]), "height": int(s["height"]), "fps": s["r_frame_rate"]}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "x"


def sidecar(raw_file: Path) -> Path:
    return raw_file.with_suffix(".meta.json")


def read_sidecar(raw_file: Path) -> dict[str, Any]:
    p = sidecar(raw_file)
    return json.loads(p.read_text()) if p.exists() else {}


def list_sources(raw_dir: Path) -> list[dict[str, Any]]:
    """Every downloaded source with its creator, url and duration (from the sidecar)."""
    out = []
    for f in sorted(raw_dir.glob("*")):
        if f.suffix.lower() not in (".mp4", ".mkv", ".webm", ".mov"):
            continue
        meta = read_sidecar(f)
        out.append({
            "source_file": f.name,
            "creator": meta.get("creator", ""),
            "source_url": meta.get("source_url", ""),
            "date": meta.get("date", ""),
            "title": meta.get("title", ""),
            "duration": meta.get("duration"),
        })
    return out


def run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-2000:]}")
