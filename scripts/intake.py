"""intake.py: download new streams from brain/intake.csv, but ONLY from approved creators.

For each row with status 'new':
  1. Resolve the video's real channel from the platform's metadata (not from the CSV).
  2. Check that channel belongs to a creator marked approved in brain/permissions.md,
     and that it is the same creator the CSV row names.
  3. Only then download it with yt-dlp into media/raw/, named <creator>_<date>_<id>.mp4,
     with a .meta.json sidecar (creator, url, duration, title).
Anything else is marked refused and logged.

`file://` sources are files a creator gave you directly (a download link, Drive, Discord).
They are accepted for approved creators without a channel lookup.

Usage: python -m scripts.intake
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from clipper import config as config_mod
from clipper.config import Config
from clipper.logs import log
from clipper.media import probe_duration, sidecar, slug
from clipper.permissions import Permissions

CSV_FIELDS = ["url", "creator", "date", "status", "note"]

Extractor = Callable[[str], dict[str, Any]]
Downloader = Callable[[str, Path, int], Path]


def ytdlp_extract(url: str) -> dict[str, Any]:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


def ytdlp_download(url: str, out_base: Path, max_height: int) -> Path:
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True,
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_base) + ".%(ext)s",
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    hits = sorted(out_base.parent.glob(out_base.name + ".*"))
    hits = [h for h in hits if not h.name.endswith(".meta.json") and not h.name.endswith(".part")]
    if not hits:
        raise RuntimeError("yt-dlp produced no file")
    return hits[0]


def channel_refs(info: dict[str, Any]) -> list[str]:
    refs = [info.get(k) for k in ("channel_url", "uploader_url")]
    if info.get("channel_id"):
        refs.append(info["channel_id"])
    host = (urlparse(info.get("webpage_url") or "").hostname or "").lower()
    if "twitch.tv" in host and info.get("uploader_id"):
        refs.append(f"https://www.twitch.tv/{info['uploader_id']}")
    if "kick.com" in host and info.get("uploader_id"):
        refs.append(f"https://kick.com/{info['uploader_id']}")
    return [r for r in refs if r]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    tmp.replace(path)


def check(url: str, csv_creator: str, perms: Permissions, extract: Extractor) -> tuple[bool, str, dict[str, Any]]:
    """The safety gate. Returns (allowed, reason, info)."""
    if url.startswith("file://"):
        c = perms.get(csv_creator)
        if c and c.is_approved():
            return True, "creator-provided file", {"creator": c.creator}
        return False, f"creator '{csv_creator}' is not approved", {}
    try:
        info = extract(url)
    except Exception as e:  # network or extractor failure
        return False, f"metadata lookup failed: {e}", {}
    refs = channel_refs(info)
    owner = perms.creator_for_channel(refs)
    if not owner:
        return False, f"channel {refs[:2]} is not on the approved list", info
    named = perms.get(csv_creator)
    if named is None or named.key != owner.key:
        return False, f"channel belongs to '{owner.creator}', but the row names '{csv_creator}'", info
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        return False, "still live or upcoming; retry later", info
    return True, "approved channel", {**info, "creator": owner.creator}


def run(cfg: Config, extract: Extractor = ytdlp_extract, download: Downloader = ytdlp_download) -> dict[str, int]:
    perms = Permissions.load(cfg.paths.permissions)
    rows = read_csv(cfg.paths.intake_csv)
    counts = {"downloaded": 0, "refused": 0, "failed": 0, "skipped": 0}
    max_h = int(cfg.get("intake", {}).get("max_height", 1080))
    max_age = int(cfg.get("intake", {}).get("max_age_hours", 72))
    for r in rows:
        if r.get("status", "new") not in ("", "new", "retry"):
            continue
        url, creator = r.get("url", ""), r.get("creator", "")
        ok, reason, info = check(url, creator, perms, extract)
        if not ok:
            r["status"] = "retry" if "still live" in reason else "refused"
            r["note"] = reason
            counts["refused"] += 1
            log(cfg, "intake", "refused", url=url, creator=creator, reason=reason)
            continue
        ts = info.get("timestamp") or info.get("release_timestamp")
        if ts and datetime.now(timezone.utc) - datetime.fromtimestamp(ts, timezone.utc) > timedelta(hours=max_age):
            r["status"], r["note"] = "skipped", f"older than {max_age}h"
            counts["skipped"] += 1
            continue
        date = r.get("date") or (info.get("upload_date") or "")[:8] or cfg.today()
        vid = slug(str(info.get("id") or Path(urlparse(url).path).stem or "src"))
        base = cfg.paths.raw / f"{slug(info['creator'])}_{slug(date)}_{vid}"
        try:
            if url.startswith("file://"):
                src = Path(urlparse(url).path)
                out = base.with_suffix(src.suffix or ".mp4")
                shutil.copy2(src, out)
            else:
                out = download(url, base, max_h)
            meta = {
                "creator": info["creator"], "source_url": url, "date": date,
                "title": info.get("title", ""), "duration": info.get("duration") or probe_duration(out),
                "channel": info.get("channel") or info.get("uploader") or "",
                "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            sidecar(out).write_text(json.dumps(meta, indent=2))
            r["status"], r["note"] = "downloaded", out.name
            counts["downloaded"] += 1
            log(cfg, "intake", "downloaded", url=url, creator=info["creator"], file=out.name)
        except Exception as e:
            r["status"], r["note"] = "failed", str(e)[:300]
            counts["failed"] += 1
            log(cfg, "intake", "failed", url=url, error=str(e))
    write_csv(cfg.paths.intake_csv, rows)
    return counts


if __name__ == "__main__":
    print(run(config_mod.load()))
