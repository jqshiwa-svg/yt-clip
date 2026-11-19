"""Run one agent by hand (for testing each agent on real inputs, build step 4).

Usage: python -m scripts.run_agent scout|writer|editor|reviewer|analyst [weekly]
"""
from __future__ import annotations

import json
import sys

from clipper import config as config_mod


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    cfg = config_mod.load()
    name = argv[0]
    if name == "scout":
        from agents.scout import run
        res = run(cfg)
    elif name == "writer":
        from agents.writer import run
        res = run(cfg)
    elif name == "editor":
        from agents.editor import run
        res = run(cfg)
    elif name == "reviewer":
        from agents.reviewer import run
        res = run(cfg)
    elif name == "analyst":
        from agents.analyst import run
        res = run(cfg, "weekly" if "weekly" in argv else "daily")
    else:
        raise SystemExit(__doc__)
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
