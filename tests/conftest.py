import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from clipper import config as config_mod  # noqa: E402
from clipper.media import run_ffmpeg  # noqa: E402

PERMS = """# Permissions

| creator | status | channels | proof_link | wording | conditions | date_checked |
|---|---|---|---|---|---|---|
| GoodStreamer | approved | https://www.youtube.com/@goodstreamer; https://www.twitch.tv/goodstreamer | https://example.com/about | "Anyone may clip my streams with credit." | credit "Clip: @goodstreamer" on screen and in description | 2026-11-19 |
| VagueStreamer | unclear | https://www.youtube.com/@vague | https://example.com/v | "ask first" | | 2026-11-19 |
| NoProof | approved | https://www.youtube.com/@noproof | | | | |
"""


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    shutil.copy(REPO / "config.yaml", root / "config.yaml")
    shutil.copytree(REPO / "agents" / "prompts", root / "agents" / "prompts") if (REPO / "agents" / "prompts").exists() else None
    (root / "assets" / "sfx").mkdir(parents=True)
    c = config_mod.load(root)
    c.data["tts"]["provider"] = "test"
    c.paths.permissions.write_text(PERMS)
    return c


@pytest.fixture
def source_video(cfg):
    """A 60s 1920x1080 test stream with audio, registered as GoodStreamer's VOD."""
    import json
    out = cfg.paths.raw / "goodstreamer_20261119_abc.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=60",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=60",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)])
    out.with_suffix(".meta.json").write_text(json.dumps({
        "creator": "GoodStreamer", "source_url": "https://www.youtube.com/watch?v=abc",
        "date": "20261119", "title": "GTA 6 day one", "duration": 60.0}))
    return out
