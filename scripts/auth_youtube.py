"""One-time YouTube sign-in. Opens a browser, then stores ONLY the refresh token in .env.

Usage: python -m scripts.auth_youtube
"""
from __future__ import annotations

from clipper import config as config_mod
from clipper.youtube import SCOPES


def main() -> None:
    from dotenv import set_key
    from google_auth_oauthlib.flow import InstalledAppFlow

    cfg = config_mod.load()
    secret_file = cfg.paths.root / (cfg.secret("YOUTUBE_CLIENT_SECRET_FILE") or "client_secret.json")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not creds.refresh_token:
        raise SystemExit("Google returned no refresh token. Remove the app's access at "
                         "myaccount.google.com/permissions and run this again.")
    env = cfg.paths.root / ".env"
    env.touch(exist_ok=True)
    set_key(str(env), "YOUTUBE_REFRESH_TOKEN", creds.refresh_token)
    print("Saved YOUTUBE_REFRESH_TOKEN to .env. Sign in with the channel's brand account if asked.")


if __name__ == "__main__":
    main()
