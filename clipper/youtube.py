"""YouTube API clients built from the client-secret file + the refresh token in .env."""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",      # delete test upload, read comments
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _client_info(cfg: Config) -> dict:
    path = cfg.paths.root / (cfg.secret("YOUTUBE_CLIENT_SECRET_FILE") or "client_secret.json")
    data = json.loads(Path(path).read_text())
    return data.get("installed") or data.get("web")


def credentials(cfg: Config):
    from google.oauth2.credentials import Credentials
    info = _client_info(cfg)
    token = cfg.secret("YOUTUBE_REFRESH_TOKEN")
    if not token:
        raise RuntimeError("No YOUTUBE_REFRESH_TOKEN in .env. Run: python -m scripts.auth_youtube")
    return Credentials(None, refresh_token=token, token_uri=info["token_uri"],
                       client_id=info["client_id"], client_secret=info["client_secret"], scopes=SCOPES)


def data_api(cfg: Config):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=credentials(cfg), cache_discovery=False)


def analytics_api(cfg: Config):
    from googleapiclient.discovery import build
    return build("youtubeAnalytics", "v2", credentials=credentials(cfg), cache_discovery=False)
