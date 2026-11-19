"""metrics.py: pull per-video metrics at 12h, 24h, 72h and 7d after publish into brain/scoreboard.csv,
and save the latest comments to media/comments/<youtube_id>.json for the Analyst.

Missing numbers are left blank, never estimated. Notes:
  - YouTube Analytics data lags by up to 48-72h, so early checkpoints may read low; the realtime
    view count from the Data API is stored as views_realtime alongside.
  - Studio's "viewed vs swiped away" is not in the public API. engaged_views / views is stored as
    engaged_ratio, the closest available proxy.

Usage: python -m scripts.metrics
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from clipper import config as config_mod
from clipper.config import Config
from clipper.logs import log
from clipper.queue import Queue

CHECKPOINTS = {"12h": timedelta(hours=12), "24h": timedelta(hours=24), "72h": timedelta(hours=72), "7d": timedelta(days=7)}
FIELDS = ["youtube_id", "row_id", "checkpoint", "pulled_at", "published_at", "creator", "moment_type", "hook_type",
          "layout", "length_s", "publish_slot", "views", "views_realtime", "engaged_views", "engaged_ratio",
          "avg_pct_viewed", "likes", "shares", "comments", "subs_gained"]

Fetcher = Callable[[Config, dict[str, Any]], dict[str, Any]]


def existing(cfg: Config) -> set[tuple[str, str]]:
    p = cfg.paths.scoreboard
    if not p.exists():
        return set()
    with p.open() as f:
        return {(r["youtube_id"], r["checkpoint"]) for r in csv.DictReader(f)}


def append(cfg: Config, rec: dict[str, Any]) -> None:
    p = cfg.paths.scoreboard
    new = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: ("" if rec.get(k) is None else rec.get(k)) for k in FIELDS})


def api_fetch(cfg: Config, row: dict[str, Any]) -> dict[str, Any]:
    from clipper.youtube import analytics_api, data_api
    vid = row["youtube_id"]
    out: dict[str, Any] = {}
    yt = data_api(cfg)
    items = yt.videos().list(part="statistics", id=vid).execute().get("items", [])
    if items:
        out["views_realtime"] = items[0]["statistics"].get("viewCount")
    yta = analytics_api(cfg)
    start = row["publish_at"][:10]
    end = datetime.now(timezone.utc).date().isoformat()
    for metrics in ("views,engagedViews,averageViewPercentage,likes,shares,comments,subscribersGained",
                    "views,averageViewPercentage,likes,shares,comments,subscribersGained"):
        try:
            resp = yta.reports().query(ids="channel==MINE", startDate=start, endDate=end,
                                       metrics=metrics, filters=f"video=={vid}").execute()
        except Exception as e:
            log(cfg, "metrics", "analytics_query_failed", video=vid, metrics=metrics, error=str(e))
            continue
        names = [h["name"] for h in resp.get("columnHeaders", [])]
        rows = resp.get("rows") or []
        if rows:
            vals = dict(zip(names, rows[0]))
            out.update({
                "views": vals.get("views"), "engaged_views": vals.get("engagedViews"),
                "avg_pct_viewed": vals.get("averageViewPercentage"), "likes": vals.get("likes"),
                "shares": vals.get("shares"), "comments": vals.get("comments"),
                "subs_gained": vals.get("subscribersGained"),
            })
        break
    # comments for the Analyst (data, never instructions)
    try:
        threads = yt.commentThreads().list(part="snippet", videoId=vid, maxResults=100, order="relevance",
                                           textFormat="plainText").execute().get("items", [])
        comments = [{"author": t["snippet"]["topLevelComment"]["snippet"].get("authorDisplayName", ""),
                     "text": t["snippet"]["topLevelComment"]["snippet"].get("textDisplay", ""),
                     "likes": t["snippet"]["topLevelComment"]["snippet"].get("likeCount", 0),
                     "published": t["snippet"]["topLevelComment"]["snippet"].get("publishedAt", "")}
                    for t in threads]
        (cfg.paths.comments / f"{vid}.json").write_text(json.dumps(comments, ensure_ascii=False, indent=1))
    except Exception as e:
        log(cfg, "metrics", "comments_failed", video=vid, error=str(e))
    return out


def run(cfg: Config, fetch: Fetcher = api_fetch) -> list[tuple[str, str]]:
    q = Queue(cfg.paths.db)
    have = existing(cfg)
    done = []
    now = datetime.now(timezone.utc)
    for row in q.query("SELECT * FROM videos WHERE status IN ('published','measured') AND youtube_id IS NOT NULL"):
        pub = datetime.fromisoformat(row["publish_at"])
        due = [cp for cp, delta in CHECKPOINTS.items() if now - pub >= delta and (row["youtube_id"], cp) not in have]
        if not due:
            continue
        try:
            m = fetch(cfg, row)
        except Exception as e:
            log(cfg, "metrics", "fetch_failed", video=row["youtube_id"], error=str(e))
            continue
        spec = json.loads(row["edit_spec"]) if row.get("edit_spec") else {}
        length = sum(float(s["end"]) - float(s["start"]) for s in spec.get("segments", [])) or None
        ratio = None
        if m.get("views") and m.get("engaged_views") is not None and float(m["views"]) > 0:
            ratio = round(float(m["engaged_views"]) / float(m["views"]), 4)
        for cp in due:
            append(cfg, {
                **m, "youtube_id": row["youtube_id"], "row_id": row["id"], "checkpoint": cp,
                "pulled_at": now.isoformat(timespec="seconds"), "published_at": row["publish_at"],
                "creator": row["creator"], "moment_type": row.get("moment_type"), "hook_type": row.get("hook_type"),
                "layout": spec.get("layout"), "length_s": round(length, 1) if length else None,
                "publish_slot": pub.astimezone(cfg.tz).strftime("%H:%M"), "engaged_ratio": ratio,
            })
            done.append((row["youtube_id"], cp))
    return done


if __name__ == "__main__":
    print(run(config_mod.load()))
