# yt-clip

A local pipeline that turns GTA 6 streams from creators who publicly allow clipping into captioned, credited YouTube Shorts with added commentary. Built from [BRIEF.md](BRIEF.md). How to run it: [RUNBOOK.md](RUNBOOK.md).

## How it fits together

```
Scout (6h) ──> brain/permissions.md, brain/intake.csv, research files
   │
intake.py ──> media/raw/        (only channels on the approved list)
transcribe.py ──> media/transcripts/
Writer ──> queue: idea -> scripted
Editor ──> edit_spec (JSON)
render.py ──> media/edits/      queue: scripted -> edited
Reviewer ──> approved / rejected (sees transcript + frames of the finished video)
[owner OK] ──> upload.py at posting times ──> published
metrics.py ──> brain/scoreboard.csv
Analyst (21:00) ──> brain/learning.md, reports/
```

- `clipper/`: shared code: config, queue (with owner-enforced status transitions), permissions parser, edit-spec validator, safety sweeps, logging and alerts.
- `scripts/`: the no-LLM scripts plus `scheduler.py`, `approve.py`, `auth_youtube.py`, `run_agent.py`.
- `agents/`: one module per agent, a shared harness (`base.py`), and the prompts in `agents/prompts/` (Part 3 + Part 4 of the brief, plus a short "how this works in code" section each).

## Safety gates enforced in code

- intake.py resolves each video's real channel from platform metadata and refuses anything not owned by an approved creator, or labelled with the wrong creator.
- A creator counts as approved only with a proof link, exact wording and a date. The Scout can't approve without today's date and can't un-remove a creator.
- render.py refuses rows with an unapproved creator, an empty credit line or a spec outside the source.
- Each agent can only write the files it owns and make the queue transitions it owns.
- upload.py only publishes `approved` rows, re-checks permission and credit, respects the daily cap, needs your OK while `require_owner_ok` is on, and stops on any unresolved takedown.
- Paused or removed creators have every unpublished row rejected on the next sweep.
- Secrets live only in .env; logs are scrubbed.

## Tests

`python -m pytest -q` runs everything with a stand-in model (no API spend) and a generated test video, including full renders of all three layouts.

## Agent model

Agents call the Claude API directly (`claude-opus-5-5`, adaptive thinking, per-agent effort in config.yaml) with the server-side refusal fallback enabled. Change models in `config.yaml` under `models:` (for example a cheaper model for the Scout).
