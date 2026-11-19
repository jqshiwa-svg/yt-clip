# Runbook

One page for the owner. Everything runs from this folder on the machine that stays on.

## One-time setup

1. Install Python 3.11+ and FFmpeg (`ffmpeg -version` should work). An NVIDIA GPU makes transcription much faster; set `whisper.device: cuda` in config.yaml if you have one.
2. `python -m venv .venv && source .venv/bin/activate` (Windows: `.venv\Scripts\activate`), then `pip install -r requirements.txt`.
3. `cp .env.example .env` and fill in `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY`. Put the Google OAuth client file in this folder as `client_secret.json`.
4. Edit config.yaml: `channel_name`, `channel_handle`, `timezone`, `posting_times`, `tts.voice_id`.
5. `python -m scripts.auth_youtube` once. Sign in with the channel's brand account. This saves only a refresh token to .env.
6. `python -m pytest -q` should pass.

## First supervised run (build steps 2 to 5)

1. Add one approved creator to brain/permissions.md by hand (or run the Scout: `python -m scripts.run_agent scout`).
2. `python -m scripts.intake` then `python -m scripts.transcribe`.
3. `python -m scripts.upload --smoke-test media/edits/<any>.mp4`: one private upload, then it deletes it.
4. Run each agent once and read what it wrote: `python -m scripts.run_agent writer`, then `editor`, `python -m scripts.render`, `python -m scripts.run_agent reviewer`.
5. `python -m scripts.scheduler --once` runs every job once while you watch.

## Start and stop

- Start: `python -m scripts.scheduler` (leave it running; use `tmux`, `screen`, a systemd service or Task Scheduler to keep it alive).
- Stop: Ctrl+C. Nothing is lost; the queue lives in queue.db.
- Pause publishing without stopping anything: add an unticked line to brain/takedowns.md, for example `- [ ] 2026-11-20 | pause | creator: - | video: - | owner pause`.

## Your daily job

- Read `reports/daily-YYYY-MM-DD.md` (written at 21:00).
- `python -m scripts.approve` lists Reviewer-approved videos waiting for you. Watch the file in media/edits/, then `python -m scripts.approve 12` to OK it, or `python -m scripts.approve --reject 12 "reason"`. Set `require_owner_ok: false` in config.yaml to skip this once you trust the output.
- Send the permission requests the Scout drafts in brain/outreach.md. When a creator says yes, put their exact wording, the link and the date in brain/permissions.md with status `approved`.
- Check the channel inbox yourself; the API can't read it.

## What each alert means

Alerts land in `reports/ALERT-YYYY-MM-DD.md` (and your webhook, if `NOTIFY_WEBHOOK_URL` is set).

| Alert | What happened | What to do |
|---|---|---|
| Publishing stopped | brain/takedowns.md has an unticked entry (claim, strike, removal request or creator objection) | Deal with it on YouTube or with the creator, then tick the line `- [x]` to resume |
| Takedown / objection logged | The Analyst found a creator objecting or a removal request in comments | Same as above; consider setting the creator to `removed` |
| Creator paused / removed | Their permission wording changed, vanished or was withdrawn. Their unpublished videos were rejected | Check their channel; only you can restore a removed creator |
| Analyst: factual error | A comment says a video got a fact wrong | Watch it; delete or correct if it's right |
| Underperformance | Three videos in a row under half the channel median at 24h | Read the weekly report; change the format, not just the topic |
| Tool outage | A job has failed for 6+ hours (API key, quota, network, disk) | Check `logs/scheduler-*.jsonl` for the error |

## Where things are

- Queue: queue.db (`sqlite3 queue.db "select id,status,title from videos order by id desc limit 20"`)
- Knowledge: brain/ (all plain Markdown and CSV)
- Finished videos: media/edits/. Downloads: media/raw/ (delete old ones to save disk)
- Logs: logs/ (one JSON line per event, per component, per day)
