import csv
import json
from types import SimpleNamespace

import pytest

from agents import analyst, editor, reviewer, scout, writer
from agents.base import Agent, Feedback, Tool, obj
from clipper import safety
from clipper.queue import Queue
from scripts import render, upload
from tests.test_pipeline_scripts import _scripted_row, _spec


# ------------------------------------------------------------- fake Claude client
def tool_use(name, input, id="t1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def reply(*blocks, stop="tool_use"):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


class FakeClient:
    """Replays scripted responses and records the tool results the harness sends back."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0) if self.responses else reply(SimpleNamespace(type="text", text="done"), stop="end_turn")

    def tool_results(self):
        out = []
        for c in self.calls:
            for m in c["messages"]:
                if m["role"] == "user" and isinstance(m["content"], list):
                    out += [b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        return out


# ------------------------------------------------------------- harness
def test_harness_blocks_writes_outside_owned_files(cfg):
    w = writer.Writer(cfg)
    with pytest.raises(Feedback):
        w.t_write_file("brain/permissions.md", "hacked")
    with pytest.raises(Feedback):
        w.t_write_file("../../etc/passwd", "x")
    with pytest.raises(Feedback):
        w.t_read_file(".env")
    assert "wrote" in w.t_write_file("brain/idea_bank.md", "# ideas")


def test_harness_stops_after_tool_fails_twice(cfg):
    class Flaky(Agent):
        name = "writer"

        def tools(self):
            def boom():
                raise RuntimeError("network down")
            return [Tool("boom", "x", obj({}), boom)]

        def first_message(self):
            return "go"

    client = FakeClient([reply(tool_use("boom", {}, "a")), reply(tool_use("boom", {}, "b")), reply(tool_use("boom", {}, "c"))])
    res = Flaky(cfg, client).run()
    assert res["stop_reason"].startswith("stopped")
    assert len(client.calls) == 2


def test_harness_sends_shared_rules_and_fallbacks(cfg):
    client = FakeClient([reply(tool_use("finish", {"summary": "nothing to do"}))])
    writer.Writer(cfg, client).run()
    call = client.calls[0]
    assert "Shared rules" in call["system"][0]["text"]
    assert cfg["channel_name"] in call["system"][0]["text"]
    assert call["model"] == "claude-opus-5-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["fallbacks"] == "default"


# ------------------------------------------------------------- scout
def test_scout_add_intake_only_for_approved(cfg):
    s = scout.Scout(cfg)
    with pytest.raises(Feedback):
        s.t_add_intake("https://youtu.be/a", "VagueStreamer", "2026-11-19")
    assert s.t_add_intake("https://youtu.be/b", "goodstreamer", "2026-11-19") == "added"
    assert s.t_add_intake("https://youtu.be/b", "GoodStreamer", "2026-11-19") == "already in intake.csv"


def test_scout_cannot_approve_without_todays_evidence_or_unremove(cfg):
    cfg.paths.permissions.write_text(cfg.paths.permissions.read_text() +
        "| Banned | removed | https://www.youtube.com/@banned | https://x | \"ok\" | | 2026-11-01 |\n")
    s = scout.Scout(cfg)
    today = cfg.today()
    table = cfg.paths.permissions.read_text()
    table = table.replace('| VagueStreamer | unclear | https://www.youtube.com/@vague | https://example.com/v | "ask first" | | 2026-11-19 |',
                          '| VagueStreamer | approved | https://www.youtube.com/@vague | https://example.com/v | "ask first" | | 2026-11-01 |')  # stale date
    table = table.replace("| Banned | removed |", "| Banned | approved |")
    table += f"| NewOne | approved | https://www.youtube.com/@newone | https://x/about | \"clip me\" | credit | {today} |\n"
    cfg.paths.permissions.write_text(table)
    fixes = s.enforce_permissions()
    from clipper.permissions import Permissions
    p = Permissions.load(cfg.paths.permissions)
    assert p.get("VagueStreamer").status == "unclear"
    assert p.get("Banned").status == "removed"
    assert p.get("NewOne").is_approved()
    assert len(fixes) == 3  # NoProof (approved without evidence) is cleaned up too


# ------------------------------------------------------------- writer
def _script(**over):
    f = dict(creator="GoodStreamer", source_file="goodstreamer_20261119_abc.mp4", clip_start=0, clip_end=50,
             credit_line="Clip: @goodstreamer", moment="boat wedged under bridge", moment_type="glitch",
             hook="This boat had one job", hook_type="bold_claim", structure="hook-moment-payoff",
             script="Line one.", title="GTA 6 boat gets stuck under a bridge",
             description="The bridge won. Clip: @goodstreamer")
    f.update(over)
    return f


def test_writer_validation(cfg, source_video):
    w = writer.Writer(cfg)
    for bad, msg in [
        (dict(creator="VagueStreamer"), "not approved"),
        (dict(title="x" * 61), "under 60"),
        (dict(description="no credit here"), "credit_line"),
        (dict(description="Clip: @goodstreamer https://my-merch.example"), "no links"),
        (dict(clip_end=75), "inside the source"),
        (dict(script="like and subscribe!"), "subscribe"),
    ]:
        with pytest.raises(Feedback, match=msg):
            w.t_create_script(**_script(**bad))
    assert "scripted" in w.t_create_script(**_script())
    with pytest.raises(Feedback, match="hook_type"):
        w.t_create_script(**_script(structure="loop"))
    assert "scripted" in w.t_create_script(**_script(hook_type="question", structure="loop"))
    with pytest.raises(Feedback, match="two Shorts from this stream"):
        w.t_create_script(**_script(hook_type="fact", structure="hook-setup-twist"))
    rows = Queue(cfg.paths.db).by_status("scripted")
    assert len(rows) == 2 and rows[0]["source_url"].startswith("https://")


# ------------------------------------------------------------- editor
def test_editor_validates_then_saves(cfg, source_video):
    q = Queue(cfg.paths.db)
    vid = _scripted_row(q, None)
    bad = _spec(hook_text="this hook text is far too long for the rule here")
    client = FakeClient([
        reply(tool_use("submit_edit_spec", {"row_id": vid, "spec": bad}, "a")),
        reply(tool_use("submit_edit_spec", {"row_id": vid, "spec": _spec()}, "b")),
    ])
    editor.run(cfg, client)
    results = client.tool_results()
    assert results[0].get("is_error") and "8 words" in results[0]["content"]
    assert json.loads(q.get(vid)["edit_spec"])["layout"] == "facecam_top"


def test_editor_rejects_repeat_layout_and_can_return_row(cfg, source_video):
    q = Queue(cfg.paths.db)
    first = _scripted_row(q, _spec(layout="crop"))
    second = _scripted_row(q, None)
    ed = editor.Editor(cfg, q.get(second))
    with pytest.raises(Feedback, match="previous video"):
        ed.t_submit_edit_spec(second, _spec(layout="crop"))
    ed.t_reject_footage(second, "no clear payoff on screen")
    assert q.get(second)["status"] == "idea"
    assert first != second


# ------------------------------------------------------------- reviewer
def _edited_row(cfg, q):
    vid = _scripted_row(q, _spec())
    q.update_fields("writer", vid, "scripted", description="The bridge won. Clip: @goodstreamer")
    render.run(cfg)
    assert q.get(vid)["status"] == "edited"
    return vid


def _fake_transcriber(cfg, src, out):
    return {"segments": [{"start": 0.2, "end": 1.4, "text": "wait what"}]}


def _review(vid, decision="approved", **checks):
    base = {f"check_{i}": True for i in range(1, 10)}
    base.update(checks)
    return {"row_id": vid, "decision": decision, "notes": "ok",
            "anti_slop_sentence": "A boat wedges under a bridge; the commentary explains why the physics broke.", **base}


def test_reviewer_approves_only_when_all_checks_pass(cfg, source_video):
    q = Queue(cfg.paths.db)
    a = _edited_row(cfg, q)
    b = _edited_row(cfg, q)
    client = FakeClient([reply(tool_use("submit_review", _review(a), "x")),
                         reply(tool_use("submit_review", _review(b, check_6=False), "y"))])
    out = reviewer.run(cfg, client, transcriber=_fake_transcriber)
    assert [o["decision"] for o in out] == ["approved", "rejected"]
    assert "check_6" in q.get(b)["review_notes"]
    # the reviewer saw frames of the finished video
    first_msg = client.calls[0]["messages"][0]["content"]
    assert sum(1 for blk in first_msg if blk.get("type") == "image") >= 4


def test_reviewer_without_verdict_rejects(cfg, source_video):
    q = Queue(cfg.paths.db)
    a = _edited_row(cfg, q)
    client = FakeClient([reply(SimpleNamespace(type="text", text="hmm"), stop="end_turn")])
    out = reviewer.run(cfg, client, transcriber=_fake_transcriber)
    assert out[0]["decision"] == "rejected" and q.get(a)["status"] == "rejected"


# ------------------------------------------------------------- upload + gates
def _approved_row(cfg, q):
    vid = _edited_row(cfg, q)
    q.transition("reviewer", vid, "approved")
    return vid


def test_upload_waits_for_owner_ok_then_publishes(cfg, source_video):
    q = Queue(cfg.paths.db)
    vid = _approved_row(cfg, q)
    sent = []
    fake = lambda c, f, body: sent.append(body) or "yt123abc"
    res = upload.run(cfg, uploader=fake)
    assert res["uploaded"] is None and res["waiting_for_owner_ok"] == [vid]
    q.set_owner_ok(vid)
    res = upload.run(cfg, uploader=fake)
    assert res["uploaded"] == vid and q.get(vid)["status"] == "published"
    st = sent[0]["status"]
    assert st["selfDeclaredMadeForKids"] is False and st["containsSyntheticMedia"] is True
    assert "Clip: @goodstreamer" in sent[0]["snippet"]["description"]


def test_takedown_blocks_upload(cfg, source_video):
    q = Queue(cfg.paths.db)
    vid = _approved_row(cfg, q)
    q.set_owner_ok(vid)
    analyst.Analyst(cfg).t_log_takedown("objection", "GoodStreamer", "yt1", "asked us to remove a clip")
    res = upload.run(cfg, uploader=lambda *a: pytest.fail("must not upload"))
    assert res["reason"] == "unresolved takedowns"
    assert list(cfg.paths.reports.glob("ALERT-*.md"))


def test_paused_creator_rejects_unpublished_rows(cfg, source_video):
    q = Queue(cfg.paths.db)
    safety.sweep(cfg, q)  # record current statuses
    vid = _scripted_row(q, _spec())
    cfg.paths.permissions.write_text(cfg.paths.permissions.read_text().replace(
        "| GoodStreamer | approved |", "| GoodStreamer | paused |"))
    res = safety.sweep(cfg, q)
    assert vid in res["rejected_unapproved"] and q.get(vid)["status"] == "rejected"
    assert "Creator paused" in next(cfg.paths.reports.glob("ALERT-*.md")).read_text()


def test_mark_measured_needs_7d_read(cfg, source_video):
    q = Queue(cfg.paths.db)
    vid = _approved_row(cfg, q)
    q.transition("upload", vid, "published", youtube_id="yt777abc", publish_at="2026-11-10T12:00:00+00:00")
    a = analyst.Analyst(cfg)
    with pytest.raises(Feedback):
        a.t_mark_measured(vid)
    with cfg.paths.scoreboard.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["youtube_id", "checkpoint", "views"])
        w.writerow(["yt777abc", "7d", "1000"])
    assert a.t_mark_measured(vid) == "measured"


def test_metrics_checkpoints_and_blanks(cfg):
    from datetime import datetime, timedelta, timezone
    from scripts import metrics
    q = Queue(cfg.paths.db)
    vid = q.create("writer", creator="GoodStreamer")
    q.transition("writer", vid, "scripted")
    q.transition("render", vid, "edited")
    q.transition("reviewer", vid, "approved")
    pub = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    q.transition("upload", vid, "published", youtube_id="ytABC123", publish_at=pub)
    done = metrics.run(cfg, fetch=lambda c, r: {"views": 500, "likes": 20})  # engaged views missing
    assert sorted(cp for _, cp in done) == ["12h", "24h"]
    rows = list(csv.DictReader(cfg.paths.scoreboard.open()))
    assert rows[0]["engaged_ratio"] == "" and rows[0]["views"] == "500"   # blank, never estimated
    assert metrics.run(cfg, fetch=lambda c, r: {"views": 1}) == []           # nothing new due
