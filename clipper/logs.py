"""JSON-lines logging to logs/, plus owner alerts in reports/ALERT-YYYY-MM-DD.md."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import requests

from .config import Config

_SECRET_PAT = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|sk_[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9_\-]+|1//[A-Za-z0-9_\-]{20,})")


def scrub(text: str) -> str:
    """Belt and braces: strip anything that looks like a key or token before it hits disk."""
    return _SECRET_PAT.sub("[REDACTED]", text)


def log(cfg: Config, component: str, event: str, **fields: Any) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "component": component, "event": event, **fields}
    line = scrub(json.dumps(rec, default=str, ensure_ascii=False))
    path = cfg.paths.logs / f"{component}-{cfg.today()}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def alert(cfg: Config, kind: str, message: str) -> None:
    """Escalate to the owner. Always writes the ALERT file; also pings the webhook if set."""
    now = cfg.now()
    path = cfg.paths.reports / f"ALERT-{now.date().isoformat()}.md"
    new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new:
            f.write(f"# Alerts for {now.date().isoformat()}\n\n")
        f.write(f"## {now.strftime('%H:%M')} {kind}\n\n{scrub(message)}\n\n")
    log(cfg, "alerts", kind, message=message)
    hook = cfg.secret("NOTIFY_WEBHOOK_URL")
    if hook:
        try:
            text = f"[{cfg.get('channel_name')}] {kind}: {message}"[:1900]
            requests.post(hook, json={"content": text, "text": text}, timeout=10)
        except requests.RequestException as e:  # a failed ping must never break the pipeline
            log(cfg, "alerts", "webhook_failed", error=str(e))
