"""Check the voice-over setup: python -m scripts.tts_test "Rule one of Vice City: never pet the alligator."

Writes media/tts/test.mp3 using the provider in config.yaml (tts.provider / tts.voice_id) and the key in .env.
"""
from __future__ import annotations

import sys

from clipper import config as config_mod
from scripts.render import tts


def main() -> None:
    cfg = config_mod.load()
    text = " ".join(sys.argv[1:]) or "Rule one of Vice City: never pet the alligator."
    out = tts(cfg, text, cfg.paths.tts / "test.mp3")
    print(f"Wrote {out}. Play it to check the voice.")


if __name__ == "__main__":
    main()
