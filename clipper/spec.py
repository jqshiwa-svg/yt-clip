"""Edit-spec validation shared by the Editor agent's tool and render.py.

All times in `narration`, `captions`, `mute` and `sfx` are seconds on the OUTPUT timeline
(0 = first frame of the finished Short). `segments` are seconds in the SOURCE file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SAFE = {"x_min": 0.06, "x_max": 0.86, "y_min": 0.12, "y_max": 0.78}  # Shorts UI-safe band, as fractions


def total_length(spec: dict[str, Any]) -> float:
    return sum(float(s["end"]) - float(s["start"]) for s in spec.get("segments", []))


def validate(spec: Any, *, row: dict[str, Any], source_duration: float | None, layouts: list[str],
             previous_layout: str | None, sfx_dir: Path, min_s: float = 20, max_s: float = 45,
             hard_max_s: float = 60, music_dir: Path | None = None) -> list[str]:
    """Return a list of problems. Empty list = valid."""
    errs: list[str] = []
    if not isinstance(spec, dict):
        return ["edit_spec must be a JSON object"]

    segs = spec.get("segments")
    if not isinstance(segs, list) or not segs or len(segs) > 4:
        errs.append("segments must be a list of 1 to 4 items")
        segs = []
    cs, ce = row.get("clip_start"), row.get("clip_end")
    for i, s in enumerate(segs):
        try:
            a, b, fx = float(s["start"]), float(s["end"]), float(s.get("focus_x", 0.5))
        except (KeyError, TypeError, ValueError):
            errs.append(f"segment {i}: needs numeric start, end, focus_x")
            continue
        if b <= a:
            errs.append(f"segment {i}: end must be after start")
        if not 0 <= fx <= 1:
            errs.append(f"segment {i}: focus_x must be between 0 and 1")
        if source_duration is not None and (a < 0 or b > source_duration + 0.01):
            errs.append(f"segment {i}: {a}-{b} is outside the source duration {source_duration:.1f}s")
        if cs is not None and ce is not None and (a < float(cs) - 0.01 or b > float(ce) + 0.01):
            errs.append(f"segment {i}: {a}-{b} is outside the row's clip window {cs}-{ce}")
    length = total_length({"segments": segs}) if not errs else 0
    if segs and not errs:
        if length < min_s or length > max_s:
            errs.append(f"total length {length:.1f}s must be between {min_s} and {max_s}s")
        if length > hard_max_s:
            errs.append(f"total length over {hard_max_s}s is never allowed")

    hook = str(spec.get("hook_text", "")).strip()
    if not hook:
        errs.append("hook_text is required")
    elif len(hook.split()) > 8:
        errs.append("hook_text must be at most 8 words")

    credit = str(spec.get("credit_text", "")).strip()
    if not credit:
        errs.append("credit_text is required")
    elif row.get("credit_line") and credit != str(row["credit_line"]).strip():
        errs.append("credit_text must exactly match the row's credit_line")

    layout = spec.get("layout")
    if layout not in layouts:
        errs.append(f"layout must be one of {layouts}")
    elif previous_layout and layout == previous_layout:
        errs.append(f"layout '{layout}' was used by the previous video; rotate to another")

    fc = spec.get("facecam")
    if fc is not None:
        try:
            x, y, w, h = (float(fc[k]) for k in ("x", "y", "w", "h"))
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
                errs.append("facecam x,y,w,h must be fractions of the source frame within 0..1")
        except (KeyError, TypeError, ValueError):
            errs.append("facecam must be null or {x, y, w, h} as fractions of the source frame")

    def _timed(name: str, items: Any, need_end: bool) -> list[dict[str, Any]]:
        if items is None:
            return []
        if not isinstance(items, list):
            errs.append(f"{name} must be a list")
            return []
        good = []
        for i, it in enumerate(items):
            try:
                st = float(it["start"])
                en = float(it["end"]) if need_end else st
            except (KeyError, TypeError, ValueError):
                errs.append(f"{name}[{i}]: needs numeric start{' and end' if need_end else ''}")
                continue
            if st < 0 or (length and en > length + 0.05) or (need_end and en <= st):
                errs.append(f"{name}[{i}]: time {st}-{en} is outside the output (0-{length:.1f}s)")
            good.append(it)
        return good

    narr = _timed("narration", spec.get("narration"), need_end=False)
    if len(narr) > 2:
        errs.append("narration: at most two commentary lines")
    for i, n in enumerate(narr):
        if not str(n.get("text", "")).strip():
            errs.append(f"narration[{i}]: text is required")

    caps = _timed("captions", spec.get("captions"), need_end=True)
    if not caps:
        errs.append("captions are required on every clip")
    for i, c in enumerate(caps):
        words = str(c.get("text", "")).split()
        if not words or len(words) > 4:
            errs.append(f"captions[{i}]: 1 to 4 words per caption group")

    _timed("mute", spec.get("mute"), need_end=True)

    sfx = spec.get("sfx") or []
    lib = sfx_dir.resolve()
    for i, s in enumerate(sfx if isinstance(sfx, list) else []):
        f = (lib / str(s.get("file", ""))).resolve()
        if lib not in f.parents or not f.is_file():
            errs.append(f"sfx[{i}]: '{s.get('file')}' is not in the approved library {sfx_dir}")

    music = spec.get("music")
    if music not in (None, False, ""):
        mlib = (music_dir or sfx_dir.parent / "music").resolve()
        mf = (mlib / str(music)).resolve()
        if mlib not in mf.parents or not mf.is_file():
            errs.append(f"music: '{music}' is not in the licensed music library {mlib}")

    db = spec.get("source_audio_db", -12)
    try:
        if not -30 <= float(db) <= 0:
            errs.append("source_audio_db must be between -30 and 0")
    except (TypeError, ValueError):
        errs.append("source_audio_db must be a number")
    return errs
