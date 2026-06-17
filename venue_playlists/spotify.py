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
    def __init__(self, access_token: str):
        self._http = httpx.Client(
            base_url=_API,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- low-level with 429 backoff ---------------------------------------

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in range(4):
            resp = self._http.request(method, path, **kw)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2")) + 1
                logger.info("Spotify 429; sleeping %ds", wait)
                time.sleep(wait)
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

    def resolve_artist(self, name: str) -> dict | None:
        """Search for an artist and guard against bad name matches."""
        data = self._get_json(
            "/search", params={"q": name, "type": "artist", "limit": 5}
        )
        items = (data or {}).get("artists", {}).get("items", [])
        if not items:
            return None
        target = _norm(name)
        # Prefer an exact normalized match; else the first hit only if it's close.
        for art in items:
            if _norm(art["name"]) == target:
                return art
        first = items[0]
        if target and (target in _norm(first["name"]) or _norm(first["name"]) in target):
            return first
        logger.info("artist resolve: no confident match for %r", name)
        return None

    def _artist_albums(self, artist_id: str, limit_releases: int = 6) -> list[dict]:
        data = self._get_json(
            f"/artists/{artist_id}/albums",
            params={"include_groups": "album,single", "market": "US", "limit": 50},
        )
        albums = (data or {}).get("items", [])
        albums.sort(key=lambda a: a.get("release_date", ""), reverse=True)
        # Dedupe albums by name (deluxe/remaster reissues).
        seen, unique = set(), []
        for a in albums:
            key = _norm(a["name"])
            if key not in seen:
                seen.add(key)
                unique.append(a)
        return unique[:limit_releases]

    def _album_tracks(self, album_id: str) -> list[dict]:
        data = self._get_json(
            f"/albums/{album_id}/tracks", params={"market": "US", "limit": 50}
        )
        return (data or {}).get("items", [])

    def _track_popularity(self, track_id: str) -> int | None:
        data = self._get_json(f"/tracks/{track_id}")
        return data.get("popularity") if data else None

    def assemble_tracks(self, artist_name: str, n: int = 4) -> list[dict]:
        """Pick ~n representative tracks for an artist.

        Popular-but-not-only-the-hit: gather candidates from recent releases,
        rank by per-track popularity, take the top n (deduped by title).
        Falls back to positional selection if popularity lookups are blocked.
        """
        artist = self.resolve_artist(artist_name)
        if not artist:
            return []

        candidates: list[dict] = []
        seen_titles: set[str] = set()
        for album in self._artist_albums(artist["id"]):
            for tr in self._album_tracks(album["id"]):
                title = _norm(tr["name"])
                if title in seen_titles or not tr.get("uri"):
                    continue
                seen_titles.add(title)
                candidates.append(tr)
            if len(candidates) >= 30:
                break
        if not candidates:
            return []

        ranked = candidates[:12]  # bound popularity lookups
        pops = {tr["id"]: self._track_popularity(tr["id"]) for tr in ranked}
        if any(p is not None for p in pops.values()):
            ranked.sort(key=lambda tr: pops.get(tr["id"]) or 0, reverse=True)
        # else: leave in release-order (newest first) as the fallback.

        chosen = ranked[:n]
        return [
            {"uri": tr["uri"], "name": tr["name"], "artist": artist["name"]}
            for tr in chosen
        ]

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
