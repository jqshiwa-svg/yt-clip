"""upload.py: at each slot, publish the oldest approved row. Never publishes anything else.

Gates (all enforced in code):
  - stops (and alerts) while brain/takedowns.md has an unresolved entry
  - only rows in status 'approved'
  - creator still approved today, credit line present in the description
  - owner OK required while config.require_owner_ok is true
  - at most max_uploads_per_day

Usage: python -m scripts.upload                      (one slot: upload the oldest eligible row)
       python -m scripts.upload --smoke-test FILE    (build step 3: private upload, then delete)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from clipper import config as config_mod
from clipper.config import Config
from clipper.logs import log
from clipper.permissions import Permissions
from clipper.queue import Queue
from clipper.safety import takedown_block

Uploader = Callable[[Config, Path, dict[str, Any]], str]


def build_body(cfg: Config, row: dict[str, Any], publish_at: datetime | None) -> dict[str, Any]:
    status: dict[str, Any] = {
        "privacyStatus": "private" if publish_at else "public",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": bool(cfg.get("synthetic_content_disclosure", True)),
    }
    if publish_at:
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "snippet": {
            "title": row["title"][:100],
            "description": row["description"],
            "categoryId": str(cfg.get("category_id", "20")),
            "defaultLanguage": cfg.get("default_language", "en"),
            "defaultAudioLanguage": cfg.get("default_language", "en"),
        },
        "status": status,
    }


def api_upload(cfg: Config, file: Path, body: dict[str, Any]) -> str:
    from googleapiclient.http import MediaFileUpload
    from clipper.youtube import data_api
    yt = data_api(cfg)
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(file), mimetype="video/mp4", resumable=True, chunksize=8 << 20))
    resp = None
    tries = 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except Exception:
            tries += 1
            if tries >= 2:     # Part 4 rule 9
                raise
    return resp["id"]


def uploads_today(cfg: Config, q: Queue) -> int:
    start = cfg.now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    return len(q.query("SELECT id FROM transitions WHERE to_status='published' AND at>=?", (start,)))


def eligible(cfg: Config, q: Queue) -> tuple[dict[str, Any] | None, str]:
    perms = Permissions.load(cfg.paths.permissions)
    for row in q.by_status("approved", order="updated_at ASC"):
        c = perms.get(row["creator"])
        if not c or not c.is_approved():
            continue  # the safety sweep will reject it
        if not row.get("credit_line") or row["credit_line"] not in (row.get("description") or ""):
            log(cfg, "upload", "skip_no_credit", row=row["id"])
            continue
        if not row.get("output_file") or not (cfg.paths.edits / row["output_file"]).is_file():
            log(cfg, "upload", "skip_missing_file", row=row["id"])
            continue
        if cfg.get("require_owner_ok", True) and not row.get("owner_ok_at"):
            continue
        return row, "ok"
    return None, "no eligible approved row"


def run(cfg: Config, uploader: Uploader | None = None) -> dict[str, Any]:
    uploader = uploader or (lambda c, f, b: api_upload(c, f, b))
    if takedown_block(cfg):
        log(cfg, "upload", "blocked_takedowns")
        return {"uploaded": None, "reason": "unresolved takedowns"}
    q = Queue(cfg.paths.db)
    if uploads_today(cfg, q) >= int(cfg.get("max_uploads_per_day", 6)):
        return {"uploaded": None, "reason": "daily upload cap reached"}
    row, why = eligible(cfg, q)
    if not row:
        waiting = [r["id"] for r in q.by_status("approved") if not r.get("owner_ok_at")]
        return {"uploaded": None, "reason": why, "waiting_for_owner_ok": waiting}
    publish_at = datetime.now(timezone.utc) + timedelta(minutes=5)  # scheduled-private, YouTube flips to public
    body = build_body(cfg, row, publish_at)
    yt_id = uploader(cfg, cfg.paths.edits / row["output_file"], body)
    q.transition("upload", row["id"], "published", note=f"youtube {yt_id}", youtube_id=yt_id,
                 publish_at=publish_at.isoformat(timespec="seconds"))
    log(cfg, "upload", "published", row=row["id"], youtube_id=yt_id)
    return {"uploaded": row["id"], "youtube_id": yt_id}


def smoke_test(cfg: Config, file: Path) -> None:
    """Build-order step 3: one private upload, confirm it exists, delete it."""
    from clipper.youtube import data_api
    body = {"snippet": {"title": "pipeline test (will be deleted)", "description": "test", "categoryId": "20"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
    vid = api_upload(cfg, file, body)
    print("uploaded private test video", vid)
    yt = data_api(cfg)
    items = yt.videos().list(part="status", id=vid).execute().get("items", [])
    print("status:", items[0]["status"] if items else "not found")
    yt.videos().delete(id=vid).execute()
    print("deleted", vid)


if __name__ == "__main__":
    cfg = config_mod.load()
    if "--smoke-test" in sys.argv:
        smoke_test(cfg, Path(sys.argv[sys.argv.index("--smoke-test") + 1]))
    else:
        print(run(cfg))
