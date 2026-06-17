"""One-time: mint a Spotify refresh token for the unattended cron.

Run this once on your machine after creating a Spotify app whose redirect URI is
exactly  http://127.0.0.1:8888/callback :

    SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy \
        uv run python scripts/mint_refresh_token.py

It opens your browser, you approve once, and it prints the REFRESH_TOKEN to store
as a GitHub Actions secret. The token is long-lived and drives the cron forever
(until you revoke the app or change your password).
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-public playlist-modify-private ugc-image-upload user-read-private"

_code: str | None = None
_state = secrets.token_urlsafe(16)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        global _code
        qs = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(qs.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if params.get("state", [""])[0] != _state:
            self.wfile.write(b"<h1>State mismatch. Try again.</h1>")
            return
        if "code" in params:
            _code = params["code"][0]
            self.wfile.write(b"<h1>Done. You can close this tab and return to the terminal.</h1>")
        else:
            err = params.get("error", ["unknown"])[0]
            self.wfile.write(f"<h1>Authorization failed: {err}</h1>".encode())

    def log_message(self, *_):  # silence the default request logging
        pass


def main() -> None:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in the environment "
            "(from your Spotify app dashboard) and re-run."
        )

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": _state,
        }
    )
    print("Opening your browser to authorize... if it doesn't open, visit:\n", auth_url)
    webbrowser.open(auth_url)

    server = HTTPServer(("127.0.0.1", 8888), _Handler)
    while _code is None:
        server.handle_request()

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = httpx.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": _code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=20,
    )
    resp.raise_for_status()
    refresh_token = resp.json()["refresh_token"]
    print("\n" + "=" * 60)
    print("SPOTIFY_REFRESH_TOKEN:")
    print(refresh_token)
    print("=" * 60)
    print("Store this as the SPOTIFY_REFRESH_TOKEN GitHub Actions secret.")


if __name__ == "__main__":
    main()
