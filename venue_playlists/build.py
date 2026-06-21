"""Full refresh: read venue calendars, rebuild each playlist, regenerate the
site data and cached cover art. Run by the daily GitHub Action (and locally).

    uv run python -m venue_playlists.build
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from venue_playlists import compose
from venue_playlists.events import ticketmaster
from venue_playlists.events.scrape import ScrapeCache, scrape_events
from venue_playlists.spotify import SpotifyClient, get_access_token

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("build")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "venues.yaml"
PLAYLIST_IDS = ROOT / "config" / "playlist_ids.json"
SCRAPE_CACHE = ROOT / "cache" / "scrape_cache.json"
TRACK_CACHE = ROOT / "cache" / "track_cache.json"
DOCS = ROOT / "docs"
DATA_OUT = DOCS / "data" / "venues.json"
COVERS_DIR = DOCS / "assets" / "covers"


def _load_env() -> None:
    """Load a local .env (KEY=VALUE per line) if present. CI uses real env."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"missing required env var: {name}")
    return val


def _playlist_ids() -> dict:
    if PLAYLIST_IDS.exists():
        return json.loads(PLAYLIST_IDS.read_text())
    return {}


def main() -> None:
    _load_env()
    config = yaml.safe_load(CONFIG.read_text())
    defaults = config.get("defaults", {})
    tracks_per_artist = defaults.get("tracks_per_artist", 4)
    max_tracks = defaults.get("max_tracks", 80)
    lookahead_days = defaults.get("lookahead_days", 120)

    tm_key = os.environ.get("TICKETMASTER_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    anthropic_client = None
    if anthropic_key:
        import anthropic

        anthropic_client = anthropic.Anthropic(api_key=anthropic_key)
    else:
        logger.warning("ANTHROPIC_API_KEY not set -- scrape-source venues will be skipped")

    token = get_access_token(
        _require("SPOTIFY_CLIENT_ID"),
        _require("SPOTIFY_CLIENT_SECRET"),
        _require("SPOTIFY_REFRESH_TOKEN"),
    )

    ids = _playlist_ids()
    cache = ScrapeCache.load(SCRAPE_CACHE)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    track_cache = json.loads(TRACK_CACHE.read_text()) if TRACK_CACHE.exists() else {}
    out_cities = []

    def save_progress() -> None:
        # Persist after every venue so an aborted run (e.g. a rate-limit cap)
        # keeps created playlists, resolved tracks, and partial site data.
        PLAYLIST_IDS.write_text(json.dumps(ids, indent=2, sort_keys=True))
        TRACK_CACHE.write_text(json.dumps(track_cache, indent=2))
        cache.save()
        DATA_OUT.write_text(
            json.dumps({"generated_at": now.isoformat(), "cities": out_cities}, indent=2)
        )

    with SpotifyClient(token) as sp, httpx.Client(timeout=30) as http:
        user_id = sp.current_user_id()
        for city in config.get("cities", []):
            tz = city["timezone"]
            out_venues = []
            out_cities.append({"name": city["name"], "venues": out_venues})
            for venue in city.get("venues", []):
                entry = _process_venue(
                    venue, city["name"], tz, lookahead_days, tracks_per_artist,
                    max_tracks, sp, user_id, http, ids, tm_key, anthropic_client,
                    cache, track_cache, now,
                )
                if entry:
                    out_venues.append(entry)
                save_progress()

    logger.info("wrote %s", DATA_OUT.relative_to(ROOT))


def _process_venue(
    venue, city_name, tz, lookahead_days, tracks_per_artist, max_tracks,
    sp: SpotifyClient, user_id, http, ids, tm_key, anthropic_client, cache,
    artist_cache, now,
) -> dict | None:
    name = venue["name"]
    slug = venue["slug"]
    source = venue.get("source", "scrape")
    logger.info("--- %s (%s)", name, source)

    # 1. gather raw events
    raw = []
    if source == "ticketmaster" and venue.get("ticketmaster_venue_id") and tm_key:
        raw = ticketmaster.upcoming_events(
            tm_key, venue["ticketmaster_venue_id"], lookahead_days=lookahead_days,
            client=http,
        )
    if not raw and venue.get("calendar_url") and anthropic_client is not None:
        raw = scrape_events(
            venue["calendar_url"], slug, anthropic_client, now.date(), cache
        )

    up = compose.upcoming_events(raw, tz, lookahead_days=lookahead_days)
    stale = not up

    # 2. assemble the ordered track list (soonest show first)
    uris, seen = [], set()
    for ev in up:
        if ev.artist not in artist_cache:
            artist_cache[ev.artist] = sp.assemble_tracks(ev.artist, n=tracks_per_artist)
        for tr in artist_cache[ev.artist]:
            if tr["uri"] not in seen:
                seen.add(tr["uri"])
                uris.append(tr["uri"])
        if len(uris) >= max_tracks:
            uris = uris[:max_tracks]
            break

    # 3. ensure playlist + write
    playlist_id = ids.get(slug)
    if not playlist_id:
        playlist_id = sp.create_playlist(
            user_id,
            compose.playlist_name(name, stale=stale),
            compose.playlist_description(name, n_shows=len(up), updated=now, stale=stale),
        )
        ids[slug] = playlist_id
        logger.info("created playlist %s for %s", playlist_id, name)

    if uris:
        sp.replace_items(playlist_id, uris)
    # When stale/empty we keep the last good tracks rather than blanking the
    # playlist -- but always update the name/description so the failure is visible.
    sp.update_details(
        playlist_id,
        compose.playlist_name(name, stale=stale),
        compose.playlist_description(name, n_shows=len(up), updated=now, stale=stale),
    )

    # 4. cache cover art (Spotify image URLs expire ~24h)
    cover_rel = _cache_cover(sp, http, playlist_id, slug)

    return {
        "name": name,
        "slug": slug,
        "city": city_name,
        "playlist_id": playlist_id,
        "playlist_url": f"https://open.spotify.com/playlist/{playlist_id}",
        "cover": cover_rel,
        "calendar_url": venue.get("calendar_url"),
        "stale": stale,
        "show_count": len(up),
        "shows": [e.as_dict() for e in up[:12]],
    }


def _cache_cover(sp: SpotifyClient, http, playlist_id: str, slug: str) -> str | None:
    url = sp.cover_image_url(playlist_id)
    if not url:
        return None
    try:
        resp = http.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("cover download failed for %s: %s", slug, exc)
        return None
    path = COVERS_DIR / f"{slug}.jpg"
    path.write_bytes(resp.content)
    return f"assets/covers/{slug}.jpg"


if __name__ == "__main__":
    main()
