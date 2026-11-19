import csv
import json

import pytest

from clipper.media import probe_duration, probe_video
from clipper.permissions import Permissions, norm_channel
from clipper.queue import Queue, TransitionError
from scripts import intake, render


# ------------------------------------------------------------- permissions
def test_permissions_require_evidence(cfg):
    p = Permissions.load(cfg.paths.permissions)
    assert p.is_approved("GoodStreamer")
    assert p.is_approved("good streamer")          # name normalisation
    assert not p.is_approved("VagueStreamer")       # unclear
    assert not p.is_approved("NoProof")             # 'approved' without proof link/wording/date
    assert not p.is_approved("Nobody")


def test_channel_normalisation():
    assert norm_channel("https://www.youtube.com/@GoodStreamer/") == norm_channel("youtube.com/@goodstreamer")
    assert norm_channel("https://m.twitch.tv/goodstreamer/videos") == "twitch:goodstreamer"
    assert norm_channel("UCabcdefghijklmnopqrstuv") == norm_channel("https://youtube.com/channel/UCabcdefghijklmnopqrstuv")


# ------------------------------------------------------------- intake gate
def _write_intake(cfg, rows):
    with cfg.paths.intake_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=intake.CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _fake_extract(info_by_url):
    return lambda url: info_by_url[url]


def test_intake_refuses_unapproved_channel(cfg):
    _write_intake(cfg, [
        {"url": "https://youtu.be/x1", "creator": "GoodStreamer", "date": "2026-11-19", "status": "new"},
        {"url": "https://youtu.be/x2", "creator": "VagueStreamer", "date": "2026-11-19", "status": "new"},
        {"url": "https://youtu.be/x3", "creator": "GoodStreamer", "date": "2026-11-19", "status": "new"},
    ])
    infos = {
        # CSV claims GoodStreamer, but the video is really on a stranger's channel
        "https://youtu.be/x1": {"id": "x1", "channel_url": "https://www.youtube.com/@stranger", "webpage_url": "https://youtube.com/watch?v=x1"},
        "https://youtu.be/x2": {"id": "x2", "channel_url": "https://www.youtube.com/@vague", "webpage_url": "https://youtube.com/watch?v=x2"},
        # NoProof's channel, labelled as GoodStreamer
        "https://youtu.be/x3": {"id": "x3", "channel_url": "https://www.youtube.com/@noproof", "webpage_url": "https://youtube.com/watch?v=x3"},
    }
    called = []
    counts = intake.run(cfg, extract=_fake_extract(infos), download=lambda *a: called.append(a))
    assert counts["refused"] == 3 and counts["downloaded"] == 0
    assert called == []                              # downloader never touched
    statuses = [r["status"] for r in intake.read_csv(cfg.paths.intake_csv)]
    assert statuses == ["refused", "refused", "refused"]


def test_intake_refuses_creator_mismatch(cfg):
    perms = Permissions.load(cfg.paths.permissions)
    ok, reason, _ = intake.check("https://youtu.be/y", "VagueStreamer", perms,
                                 lambda u: {"channel_url": "https://www.youtube.com/@goodstreamer"})
    assert not ok and "row names" in reason


def test_intake_downloads_approved_channel(cfg, tmp_path):
    _write_intake(cfg, [{"url": "https://www.twitch.tv/videos/123", "creator": "GoodStreamer", "date": "2026-11-19", "status": "new"}])
    infos = {"https://www.twitch.tv/videos/123": {
        "id": "v123", "uploader_id": "goodstreamer", "webpage_url": "https://www.twitch.tv/videos/123",
        "title": "GTA 6", "duration": 42.0}}

    def fake_download(url, base, max_h):
        out = base.with_suffix(".mp4")
        out.write_bytes(b"fake")
        return out

    counts = intake.run(cfg, extract=_fake_extract(infos), download=fake_download)
    assert counts["downloaded"] == 1
    files = list(cfg.paths.raw.glob("*.mp4"))
    assert len(files) == 1 and files[0].name.startswith("goodstreamer_2026-11-19_v123")
    meta = json.loads(files[0].with_suffix(".meta.json").read_text())
    assert meta["creator"] == "GoodStreamer" and meta["duration"] == 42.0


# ------------------------------------------------------------- queue
def test_queue_transitions_are_owned(cfg):
    q = Queue(cfg.paths.db)
    vid = q.create("writer", creator="GoodStreamer")
    with pytest.raises(TransitionError):
        q.transition("upload", vid, "published")     # not approved yet
    with pytest.raises(TransitionError):
        q.transition("reviewer", vid, "approved")    # reviewer only acts on edited
    with pytest.raises(TransitionError):
        q.create("editor", creator="x")              # editor can't create rows
    q.transition("writer", vid, "scripted")
    q.transition("render", vid, "edited")
    q.transition("reviewer", vid, "approved")
    q.transition("upload", vid, "published")
    assert q.get(vid)["status"] == "published"


# ------------------------------------------------------------- render
def _spec(**over):
    s = {
        "segments": [{"start": 5, "end": 17, "focus_x": 0.4}, {"start": 30, "end": 42, "focus_x": 0.6}],
        "facecam": {"x": 0.75, "y": 0.7, "w": 0.25, "h": 0.3},
        "narration": [{"start": 2.0, "text": "This bridge was not built for boats."}],
        "captions": [{"start": 0.2, "end": 1.4, "text": "wait what"},
                     {"start": 6.0, "end": 7.5, "text": "no way"},
                     {"start": 15.0, "end": 16.5, "text": "it's actually stuck"}],
        "hook_text": "Boat vs bridge, bridge wins",
        "credit_text": "Clip: @goodstreamer",
        "source_audio_db": -12,
        "mute": [{"start": 20.0, "end": 20.6}],
        "sfx": [],
        "layout": "facecam_top",
    }
    s.update(over)
    return s


def _scripted_row(q, spec):
    vid = q.create("writer", creator="GoodStreamer", credit_line="Clip: @goodstreamer",
                   source_file="goodstreamer_20261119_abc.mp4", clip_start=0, clip_end=60,
                   title="GTA 6 boat gets stuck under a bridge")
    q.transition("writer", vid, "scripted")
    q.update_fields("editor", vid, "scripted", edit_spec=spec)
    return vid


@pytest.mark.parametrize("layout", ["facecam_top", "crop", "blur_fit"])
def test_render_end_to_end(cfg, source_video, layout):
    q = Queue(cfg.paths.db)
    vid = _scripted_row(q, _spec(layout=layout))
    res = render.run(cfg)
    assert res["rendered"] == [vid], res
    row = q.get(vid)
    assert row["status"] == "edited"
    out = cfg.paths.edits / row["output_file"]
    v = probe_video(out)
    assert (v["width"], v["height"]) == (1080, 1920)
    assert abs(probe_duration(out) - 24.0) < 0.6


def test_render_refuses_unapproved_or_no_credit(cfg, source_video):
    q = Queue(cfg.paths.db)
    vid = _scripted_row(q, _spec())
    q.update_fields("writer", vid, "scripted", credit_line="")
    render.run(cfg)
    assert q.get(vid)["status"] == "rejected"
    assert "credit_line" in q.get(vid)["review_notes"]

    vid2 = _scripted_row(q, _spec())
    q.update_fields("writer", vid2, "scripted", creator="VagueStreamer")
    render.run(cfg)
    assert q.get(vid2)["status"] == "rejected"


def test_render_rejects_segments_outside_source(cfg, source_video):
    q = Queue(cfg.paths.db)
    vid = _scripted_row(q, _spec(segments=[{"start": 50, "end": 75, "focus_x": 0.5}]))
    render.run(cfg)
    assert q.get(vid)["status"] == "rejected"
    assert "outside" in q.get(vid)["review_notes"]
