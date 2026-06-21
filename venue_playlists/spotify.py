"""Spotify Web API client.

Scoped to what a *new* development-mode app can still do in 2026. Notably it
does NOT use `top-tracks`, `recommendations`, audio-features, or editorial
"This Is" playlists -- all removed for new apps. Track lists are assembled from
Search -> Artist's Albums -> Album Tracks and ranked by per-track popularity.
"""

from __future__ import annotations

import base64
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_ACCOUNTS = "https://accounts.spotify.com/api/token"
_API = "https://api.spotify.com/v1"

# Scopes requested at mint time. Includes ugc-image-upload up front so we never
# need to re-mint (a scope change invalidates the refresh token).
SCOPES = "playlist-modify-public playlist-modify-private ugc-image-upload user-read-private"

_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*")
_DASH_SUFFIX = re.compile(r"\s*-\s*(remaster|live|mono|mix|version|edit|acoustic).*$", re.I)


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange the long-lived refresh token for a 1-hour access token."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = httpx.post(
        _ACCOUNTS,
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("refresh_token") and body["refresh_token"] != refresh_token:
        # Authorization-Code refresh tokens rarely rotate, but warn loudly if one
        # does so the operator can update the stored secret.
        logger.warning(
            "Spotify returned a NEW refresh token -- update the SPOTIFY_REFRESH_TOKEN "
            "secret or the next run will fail auth."
        )
    return body["access_token"]


def _norm(name: str) -> str:
    name = _PAREN.sub(" ", name)
    name = _DASH_SUFFIX.sub("", name)
    return re.sub(r"\s+", " ", name).strip().lower()


class SpotifyClient:
    def __init__(self, access_token: str, min_interval: float = 0.4):
        self._http = httpx.Client(
            base_url=_API,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        # Dev-mode quota is strict and punishes bursts with very long Retry-After
        # values, so we self-throttle to stay well under the rolling window.
        self._min_interval = min_interval
        self._last_call = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- low-level with 429 backoff ---------------------------------------

    # If Spotify asks us to wait longer than this, abort the run rather than
    # hang for hours -- a later cron run will pick up where we left off.
    MAX_BACKOFF = 60

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for _ in range(4):
            gap = time.monotonic() - self._last_call
            if gap < self._min_interval:
                time.sleep(self._min_interval - gap)
            resp = self._http.request(method, path, **kw)
            self._last_call = time.monotonic()
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2"))
                if wait > self.MAX_BACKOFF:
                    raise RuntimeError(
                        f"Spotify rate limit Retry-After={wait}s exceeds cap; aborting run"
                    )
                logger.info("Spotify 429; sleeping %ds", wait + 1)
                time.sleep(wait + 1)
                continue
            return resp
        return resp

    def _get_json(self, path: str, **kw) -> dict | None:
        resp = self._request("GET", path, **kw)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Spotify GET %s -> %s", path, resp.status_code)
        return None

    # --- account ----------------------------------------------------------

    def current_user_id(self) -> str:
        data = self._get_json("/me")
        if not data:
            raise RuntimeError("could not read current user (check token/scopes)")
        return data["id"]

    # --- track assembly ---------------------------------------------------

    def assemble_tracks(self, artist_name: str, n: int = 3) -> list[dict]:
        """Pick the artist's ~n most popular tracks in ONE search call.

        A combined `type=artist,track` search returns the artist (for an
        identity guard) and full track objects (with popularity) in a single
        request -- so we rank by popularity for free, avoid the per-track and
        per-album-tracks endpoints entirely (the latter has a punishing
        dev-mode quota), and keep the cold build to ~one call per artist.
        """
        data = self._get_json(
            "/search", params={"q": artist_name, "type": "artist,track", "limit": 10}
        )
        if not data:
            return []

        target = _norm(artist_name)
        artists = data.get("artists", {}).get("items", [])
        artist = next((a for a in artists if _norm(a["name"]) == target), None)
        if artist is None and artists:
            first = artists[0]
            if target in _norm(first["name"]) or _norm(first["name"]) in target:
                artist = first
        if artist is None:
            logger.info("artist resolve: no confident match for %r", artist_name)
            return []
        aid, aname = artist["id"], artist["name"]

        tracks = data.get("tracks", {}).get("items", [])
        mine = [t for t in tracks if any(ar.get("id") == aid for ar in t.get("artists", []))]
        mine.sort(key=lambda t: t.get("popularity", 0), reverse=True)

        chosen, seen = [], set()
        for t in mine:
            title = _norm(t["name"])
            if title in seen or not t.get("uri"):
                continue
            seen.add(title)
            chosen.append({"uri": t["uri"], "name": t["name"], "artist": aname})
            if len(chosen) >= n:
                break
        return chosen

    # --- playlist writes --------------------------------------------------

    def create_playlist(self, user_id: str, name: str, description: str) -> str:
        resp = self._request(
            "POST",
            "/me/playlists",
            json={"name": name, "public": True, "description": description},
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def replace_items(self, playlist_id: str, uris: list[str]) -> None:
        """Replace the entire playlist. Spotify caps each call at 100 URIs."""
        first, rest = uris[:100], uris[100:]
        resp = self._request("PUT", f"/playlists/{playlist_id}/items", json={"uris": first})
        resp.raise_for_status()
        for i in range(0, len(rest), 100):
            chunk = rest[i : i + 100]
            r = self._request("POST", f"/playlists/{playlist_id}/items", json={"uris": chunk})
            r.raise_for_status()

    def update_details(self, playlist_id: str, name: str, description: str) -> None:
        resp = self._request(
            "PUT", f"/playlists/{playlist_id}", json={"name": name, "description": description}
        )
        resp.raise_for_status()

    def cover_image_url(self, playlist_id: str) -> str | None:
        """Best (largest) cover image URL for the playlist. URLs expire ~24h."""
        data = self._get_json(f"/playlists/{playlist_id}", params={"fields": "images"})
        images = (data or {}).get("images") or []
        if not images:
            return None
        # Spotify returns largest first, but sort defensively (height may be null).
        images = sorted(images, key=lambda im: (im.get("height") or 0), reverse=True)
        return images[0].get("url")
