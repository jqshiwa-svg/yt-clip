"""transcribe.py: run faster-whisper on new files in media/raw/, write word-level JSON to media/transcripts/.

Usage: python -m scripts.transcribe          (one pass over media/raw)
       python -m scripts.transcribe --watch  (keep watching)
"""
from __future__ import annotations

import json
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from clipper import config as config_mod
from clipper.config import Config
from clipper.logs import log

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov")


@lru_cache(maxsize=2)
def _model(name: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device=device, compute_type=compute_type)


def transcribe_file(cfg: Config, src: Path, out: Path) -> dict[str, Any]:
    w = cfg.get("whisper", {})
    model = _model(w.get("model", "small"), w.get("device", "cpu"), w.get("compute_type", "int8"))
    segments, info = model.transcribe(str(src), word_timestamps=True, vad_filter=True)
    data: dict[str, Any] = {"file": src.name, "language": info.language, "duration": info.duration, "segments": []}
    for s in segments:
        data["segments"].append({
            "start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
            "words": [{"start": round(x.start, 2), "end": round(x.end, 2), "word": x.word.strip(),
                       "p": round(x.probability, 3)} for x in (s.words or [])],
        })
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(out)
    return data


def transcript_path(cfg: Config, src: Path) -> Path:
    return cfg.paths.transcripts / (src.stem + ".json")


def run_once(cfg: Config) -> list[str]:
    done = []
    for src in sorted(cfg.paths.raw.iterdir()):
        if src.suffix.lower() not in VIDEO_EXT:
            continue
        out = transcript_path(cfg, src)
        if out.exists():
            continue
        try:
            log(cfg, "transcribe", "start", file=src.name)
            transcribe_file(cfg, src, out)
            log(cfg, "transcribe", "done", file=src.name)
            done.append(src.name)
        except Exception as e:
            log(cfg, "transcribe", "failed", file=src.name, error=str(e))
    return done


if __name__ == "__main__":
    cfg = config_mod.load()
    if "--watch" in sys.argv:
        while True:
            run_once(cfg)
            time.sleep(60)
    else:
        print(run_once(cfg))
