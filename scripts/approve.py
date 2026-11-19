"""Owner approval gate. Lists Reviewer-approved videos waiting for you, and OKs them for upload.

Usage: python -m scripts.approve            (list what's waiting)
       python -m scripts.approve 12 13      (OK rows 12 and 13 for upload)
       python -m scripts.approve --reject 14 "reason"   (send one back to the Writer)
"""
from __future__ import annotations

import sys

from clipper import config as config_mod
from clipper.queue import Queue


def main(argv: list[str]) -> None:
    cfg = config_mod.load()
    q = Queue(cfg.paths.db)
    if argv and argv[0] == "--reject":
        vid, reason = int(argv[1]), " ".join(argv[2:]) or "owner rejected"
        # owner rejections use the safety actor (approved -> rejected)
        q.transition("safety", vid, "rejected", note=f"owner: {reason}", review_notes=f"[owner] {reason}",
                     rejected_by="owner")
        print(f"rejected {vid}")
        return
    if argv:
        for a in argv:
            q.set_owner_ok(int(a))
            print(f"OK for upload: {a}")
        return
    rows = [r for r in q.by_status("approved") if not r.get("owner_ok_at")]
    if not rows:
        print("Nothing waiting for your OK.")
    for r in rows:
        print(f"#{r['id']}  {r['title']}\n     creator: {r['creator']}   file: media/edits/{r['output_file']}\n"
              f"     reviewer: {(r['review_notes'] or '').strip()[:200]}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
