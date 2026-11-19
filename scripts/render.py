"""render.py: turn a scripted row with an edit_spec into a finished 1080x1920 Short.

Steps: TTS for the commentary lines, cut the segments, crop/stack to vertical per the layout,
burn hook text, on-screen credit and word-group captions (ASS), duck the source audio under
narration, mute flagged ranges, mix, normalise loudness, write to media/edits/. Status -> edited.

Gates: refuses any row whose creator is not approved or whose credit_line is empty.

Usage: python -m scripts.render            (all waiting rows)
       python -m scripts.render --id 12    (one row)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests

from clipper import config as config_mod
from clipper import spec as spec_mod
from clipper.config import Config
from clipper.logs import log
from clipper.media import probe_duration, run_ffmpeg, slug
from clipper.permissions import Permissions
from clipper.queue import Queue
from clipper.safety import source_duration


class RenderRefused(Exception):
    pass


# ---------------------------------------------------------------- TTS
def tts(cfg: Config, text: str, out: Path) -> Path:
    t = cfg.get("tts", {})
    provider = t.get("provider", "elevenlabs")
    if provider == "test":
        dur = max(1.0, 0.36 * len(text.split()))
        run_ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=660:duration={dur:.2f}", "-ac", "2", "-ar", "48000", str(out)])
        return out
    if provider == "flite":  # offline robotic voice built into FFmpeg; for previews, not for publishing
        txt = out.with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")
        path = str(txt).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        run_ffmpeg(["-f", "lavfi", "-i", f"flite=textfile='{path}':voice={t.get('flite_voice', 'slt')}",
                    "-ac", "2", "-ar", "48000", str(out)])
        return out
    if provider == "elevenlabs":
        key = cfg.secret("ELEVENLABS_API_KEY")
        voice = t.get("voice_id")
        if not key or not voice:
            raise RenderRefused("ELEVENLABS_API_KEY (.env) and tts.voice_id (config.yaml) are required")
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": key, "accept": "audio/mpeg"},
            json={"text": text, "model_id": t.get("model_id", "eleven_multilingual_v2")},
            timeout=60,
        )
        if r.status_code != 200:
            raise RuntimeError(f"TTS failed with HTTP {r.status_code}: {r.text[:200]}")
        out.write_bytes(r.content)
        return out
    raise RenderRefused(f"unknown tts provider {provider}")


# ---------------------------------------------------------------- captions (ASS)
def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return str(text).replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def build_ass(cfg: Config, spec: dict[str, Any], length: float) -> str:
    W, H = cfg["render"]["width"], cfg["render"]["height"]
    font = cfg["render"].get("font", "DejaVu Sans")
    ml, mr = int(W * spec_mod.SAFE["x_min"]) + 15, int(W * (1 - spec_mod.SAFE["x_max"])) + 15
    cap_v = int(H * (1 - 0.70))          # caption baseline at ~70% of frame height
    credit_v = int(H * spec_mod.SAFE["y_min"]) + 10
    # hook: "middle" (default) sits in the centre of the frame, clear of a facecam at the top
    if cfg["render"].get("hook_position", "middle") == "top":
        hook_align, hook_v = 8, credit_v + 80
    else:
        hook_align, hook_v = 5, 0
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,{ml},{mr},{cap_v},1
Style: Hook,{font},80,&H0000E5FF,&H0000E5FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,4,0,{hook_align},{ml},{mr},{hook_v},1
Style: Credit,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,3,3,0,8,{ml},{mr},{credit_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = [
        f"Dialogue: 2,{_ts(0)},{_ts(min(1.5, length))},Hook,,0,0,0,,{_esc(spec['hook_text'])}",
        # credit stays on screen for the whole Short (brief: at least the first 3 seconds)
        f"Dialogue: 1,{_ts(0)},{_ts(length)},Credit,,0,0,0,,{_esc(spec['credit_text'])}",
    ]
    for c in spec.get("captions", []):
        st = float(c["start"])
        if st < 1.5:  # don't stack captions under the hook
            st = max(st, 0.0)
        ev.append(f"Dialogue: 0,{_ts(st)},{_ts(float(c['end']))},Caption,,0,0,0,,{_esc(c['text'])}")
    return head + "\n".join(ev) + "\n"


# ---------------------------------------------------------------- filtergraph
def _video_chain(idx: int, seg: dict[str, Any], spec: dict[str, Any], W: int, H: int, fps: int) -> str:
    fx = float(seg.get("focus_x", 0.5))
    layout = spec["layout"]
    fc = spec.get("facecam")
    cx = lambda ow: f"'min(max(iw*{fx}-{ow // 2}\\,0)\\,iw-{ow})'"
    tail = f"setsar=1,fps={fps},format=yuv420p[v{idx}]"
    if layout == "facecam_top" and fc:
        top_h = int(H / 3)
        bot_h = H - top_h
        return (f"[{idx}:v]split=2[fa{idx}][fb{idx}];"
                f"[fa{idx}]crop=iw*{fc['w']}:ih*{fc['h']}:iw*{fc['x']}:ih*{fc['y']},"
                f"scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h}[ft{idx}];"
                f"[fb{idx}]scale={W}:{bot_h}:force_original_aspect_ratio=increase,crop={W}:{bot_h}:{cx(W)}:0[fg{idx}];"
                f"[ft{idx}][fg{idx}]vstack=inputs=2,{tail}")
    if layout == "blur_fit":
        return (f"[{idx}:v]split=2[ba{idx}][bb{idx}];"
                f"[ba{idx}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2[bg{idx}];"
                f"[bb{idx}]scale={W}:-2[fg{idx}];"
                f"[bg{idx}][fg{idx}]overlay=(W-w)/2:(H-h)/2,{tail}")
    # crop (also the fallback for facecam_top without a facecam region)
    return f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:{cx(W)}:0,{tail}"


def _enable(windows: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t\\,{a:.2f}\\,{b:.2f})" for a, b in windows)


def pick_music(cfg: Config, spec: dict[str, Any], row_id: int) -> Path | None:
    """The spec's track if it names one, else rotate through the music library by row id."""
    m = cfg.get("music", {}) or {}
    if not m.get("enabled", False) or spec.get("music") is False:
        return None
    lib = (cfg.paths.root / m.get("dir", "assets/music")).resolve()
    if spec.get("music"):
        return lib / str(spec["music"])
    tracks = sorted(p for p in lib.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac"))
    return tracks[row_id % len(tracks)] if tracks else None


def build_command(cfg: Config, src: Path, spec: dict[str, Any], narr_files: list[tuple[Path, float, float]],
                  ass_path: Path, out: Path, music: Path | None = None) -> list[str]:
    W, H, fps = cfg["render"]["width"], cfg["render"]["height"], cfg["render"]["fps"]
    segs = spec["segments"]
    args: list[str] = []
    for s in segs:
        a, b = float(s["start"]), float(s["end"])
        args += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
    n = len(segs)
    for f, _, _ in narr_files:
        args += ["-i", str(f)]
    sfx = spec.get("sfx") or []
    sfx_dir = (cfg.paths.root / cfg["render"].get("sfx_dir", "assets/sfx")).resolve()
    for s in sfx:
        args += ["-i", str(sfx_dir / s["file"])]
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]

    parts = [_video_chain(i, s, spec, W, H, fps) for i, s in enumerate(segs)]
    parts += [f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]" for i in range(n)]
    parts.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    ass = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    parts.append(f"[vc]ass='{ass}'[vout]")

    chain = "[ac]"
    duck = [(st, st + d) for _, st, d in narr_files]
    db = float(spec.get("source_audio_db", -12))
    audio_steps = []
    if duck:
        audio_steps.append(f"volume=enable='{_enable(duck)}':volume={db}dB")
    mute = [(float(m["start"]), float(m["end"])) for m in spec.get("mute") or []]
    if mute:
        audio_steps.append(f"volume=enable='{_enable(mute)}':volume=0")
    parts.append(f"{chain}{','.join(audio_steps) or 'anull'}[ad]")

    mix_in = ["[ad]"]
    for j, (_, st, _) in enumerate(narr_files):
        ms = int(st * 1000)
        parts.append(f"[{n + j}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[n{j}]")
        mix_in.append(f"[n{j}]")
    for k, s in enumerate(sfx):
        ms = int(float(s["start"]) * 1000)
        idx = n + len(narr_files) + k
        parts.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},volume=-6dB[s{k}]")
        mix_in.append(f"[s{k}]")
    if music:
        length = spec_mod.total_length(spec)
        mcfg = cfg.get("music", {}) or {}
        mdb = float(spec.get("music_db", mcfg.get("volume_db", -22)))
        extra = float(mcfg.get("duck_under_narration_db", -8))
        idx = n + len(narr_files) + len(sfx)
        steps = [f"atrim=0:{length:.2f}", "asetpts=PTS-STARTPTS", "aresample=48000", "aformat=channel_layouts=stereo",
                 "afade=t=in:d=0.6", f"afade=t=out:st={max(0.0, length - 1.5):.2f}:d=1.5", f"volume={mdb}dB"]
        if duck:
            steps.append(f"volume=enable='{_enable(duck)}':volume={extra}dB")
        parts.append(f"[{idx}:a]{','.join(steps)}[mus]")
        mix_in.append("[mus]")
    lufs = cfg["render"].get("loudness_lufs", -14)
    parts.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0:duration=first,"
                 f"loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000[aout]")

    return args + [
        "-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out),
    ]


# ---------------------------------------------------------------- row processing
def check_row(cfg: Config, row: dict[str, Any], perms: Permissions) -> tuple[Path, dict[str, Any]]:
    c = perms.get(row["creator"])
    if not c or not c.is_approved():
        raise RenderRefused(f"creator '{row['creator']}' is not approved")
    if not (row.get("credit_line") or "").strip():
        raise RenderRefused("credit_line is empty")
    if not row.get("edit_spec"):
        raise RenderRefused("no edit_spec")
    spec = json.loads(row["edit_spec"])
    src = cfg.paths.raw / Path(row["source_file"] or "").name
    if not src.is_file():
        raise RenderRefused(f"source file {row['source_file']} not found")
    r = cfg["render"]
    errs = spec_mod.validate(
        spec, row=row, source_duration=source_duration(cfg, row["source_file"]),
        layouts=list(cfg["layouts"].keys()), previous_layout=None,
        sfx_dir=cfg.paths.root / r.get("sfx_dir", "assets/sfx"),
        music_dir=cfg.paths.root / (cfg.get("music", {}) or {}).get("dir", "assets/music"),
        min_s=r.get("min_seconds", 20), max_s=r.get("max_seconds", 45),
    )
    if errs:
        raise RenderRefused("invalid edit_spec: " + "; ".join(errs))
    return src, spec


def render_row(cfg: Config, q: Queue, row: dict[str, Any], perms: Permissions) -> Path:
    src, spec = check_row(cfg, row, perms)
    length = spec_mod.total_length(spec)
    out = cfg.paths.edits / f"{row['id']:05d}_{slug(row['creator'])}_{slug(row.get('title') or 'short')}.mp4"
    with tempfile.TemporaryDirectory(dir=cfg.paths.tts) as td:
        tdp = Path(td)
        narr = []
        for j, line in enumerate(spec.get("narration") or []):
            f = tts(cfg, line["text"], tdp / f"n{j}.mp3")
            d = probe_duration(f)
            st = float(line["start"])
            if st + d > length:
                raise RenderRefused(f"narration line {j} ({d:.1f}s from {st}s) runs past the end of the video")
            narr.append((f, st, d))
        ass = tdp / "subs.ass"
        ass.write_text(build_ass(cfg, spec, length), encoding="utf-8")
        tmp_out = out.with_suffix(".part.mp4")
        music = pick_music(cfg, spec, int(row["id"]))
        run_ffmpeg(build_command(cfg, src, spec, narr, ass, tmp_out, music))
        tmp_out.replace(out)
    q.transition("render", row["id"], "edited", note=f"rendered {out.name}", output_file=out.name)
    log(cfg, "render", "rendered", row=row["id"], file=out.name, seconds=round(length, 2))
    return out


def run(cfg: Config, only_id: int | None = None) -> dict[str, list]:
    q = Queue(cfg.paths.db)
    perms = Permissions.load(cfg.paths.permissions)
    res: dict[str, list] = {"rendered": [], "refused": [], "failed": []}
    rows = [r for r in q.by_status("scripted") if r.get("edit_spec")]
    if only_id is not None:
        rows = [r for r in rows if r["id"] == only_id]
    for row in rows:
        try:
            render_row(cfg, q, row, perms)
            res["rendered"].append(row["id"])
        except RenderRefused as e:
            q.transition("safety", row["id"], "rejected", note=f"render refused: {e}",
                         review_notes=f"[render] {e}", rejected_by="render")
            log(cfg, "render", "refused", row=row["id"], reason=str(e))
            res["refused"].append(row["id"])
        except (RuntimeError, subprocess.SubprocessError, requests.RequestException, OSError) as e:
            attempts = int(row.get("attempts") or 0) + 1
            log(cfg, "render", "failed", row=row["id"], error=str(e), attempts=attempts)
            if attempts >= 2:  # Part 4 rule 9: a tool that fails twice stops; don't loop forever
                q.transition("safety", row["id"], "rejected", note=f"render failed twice: {e}",
                             review_notes=f"[render] failed twice: {str(e)[:300]}", rejected_by="render")
            else:
                q.update_fields("render", row["id"], "scripted", attempts=attempts)
            res["failed"].append(row["id"])
    return res


if __name__ == "__main__":
    only = int(sys.argv[sys.argv.index("--id") + 1]) if "--id" in sys.argv else None
    print(run(config_mod.load(), only))
