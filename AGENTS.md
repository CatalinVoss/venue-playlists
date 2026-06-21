# AGENTS.md – maintaining Venue Playlists

Live: **https://catalinvoss.github.io/venue-playlists/** · one auto-maintained
Spotify playlist per live-music venue, holding the upcoming acts (soonest first),
refreshed daily by a GitHub Action. Runs at ~$0.

This file is the contract for changing this repo. Keep it true: if you change a
behavior or a choice below, update the matching line here in the same commit.

## How it works

```
GitHub Action (daily cron, 06:17 UTC)         python -m venue_playlists.build
        │
  for each venue in config/venues.yaml:
    Ticketmaster Discovery API ──► upcoming shows {artist, date, ticket_url}
      (or scrape calendar HTML ──► Claude Haiku extract, if source: scrape)
        │  keep upcoming-only, soonest first        (compose.upcoming_events)
    per artist: Spotify search ──► top tracks        (spotify.assemble_tracks)
        │  round-robin across artists, cap to N
    create/replace the venue's playlist + cover      (spotify.SpotifyClient)
        │
  write docs/data/venues.json + docs/assets/covers/*.jpg, commit + push
        │
GitHub Pages serves docs/ ──► the static page fetches venues.json and renders cards
```

No server, no database. Generated data is committed back into the repo; the page
reads it as plain files. State that must persist across runs lives in the repo
(`config/playlist_ids.json`, `cache/*.json`, `docs/data`, `docs/assets/covers`).

## Repo map

| Path | Role |
|---|---|
| `config/venues.yaml` | **The control surface.** Cities, venues, source, IDs, tuning defaults. Hand-edited. |
| `config/playlist_ids.json` | slug → Spotify playlist id. **Machine-written**, committed. Do not hand-edit. |
| `venue_playlists/build.py` | Orchestrator: read config → per-venue refresh → write site data. Saves after every venue. |
| `venue_playlists/spotify.py` | Spotify client: auth, track assembly, playlist writes, cover read. Throttled. |
| `venue_playlists/events/ticketmaster.py` | Ticketmaster Discovery API client. |
| `venue_playlists/events/scrape.py` | HTML calendar → trafilatura → Claude Haiku → events; content-hash cache. |
| `venue_playlists/compose.py` | Upcoming-only filter (+5pm rule), playlist name/description. |
| `venue_playlists/models.py` | `Event` dataclass – the one shared shape. |
| `cache/track_cache.json` | artist name → chosen tracks. Skips re-resolving artists across runs. |
| `cache/scrape_cache.json` | venue slug → {page hash, events}. Skips the LLM call when a page is unchanged. |
| `scripts/mint_refresh_token.py` | One-time local OAuth to mint the Spotify refresh token. |
| `docs/` | The GitHub Pages site: `index.html`, `style.css`, generated `data/` + `assets/covers/`. |
| `.github/workflows/refresh.yml` | Daily cron + `workflow_dispatch`; runs the build and commits the output. |

## Design choices (and why)

- **GitHub-native, $0.** Pages (public repo, `main` `/docs`) + Actions cron +
  JSON-committed-to-repo. The dataset is tiny, read-mostly, rewritten once a day
  – a real DB would be pure overhead. Git history doubles as free versioning.
- **Ticketmaster first, scrape as fallback.** TM Discovery API is free and
  covers most mid-size venues; for venues it misses, a generic
  fetch→clean→Haiku-extract path reads the venue's own calendar. The scraper is
  the only thing that costs money (~$1–5/mo) and only runs for `source: scrape`
  venues (none today – all current venues are on Ticketmaster).
- **Tracks come from Search, not top-tracks.** Spotify's 2026 dev-mode changes
  removed `top-tracks`, `recommendations`, audio-features, and editorial "This
  Is" playlists for new apps – *and* slapped a punishing per-endpoint quota on
  `GET /albums/{id}/tracks`. `assemble_tracks` instead does **one**
  `GET /search?type=artist,track` per artist: it returns the artist (for an
  identity guard) and full track objects *with popularity*, so we rank for free,
  stay at ~1 call/artist, and never touch the throttled endpoints. See Gotchas.
- **Rate-limit discipline is load-bearing.** `SpotifyClient` self-throttles
  (`min_interval` 0.4s) and **aborts** if a 429 asks us to wait > `MAX_BACKOFF`
  (60s) rather than sleeping for the ~24h dev-mode penalty. Combined with the
  disk caches, a steady-state daily run is a few dozen calls.
- **Resumable by construction.** `build.save_progress()` writes ids, caches, and
  `venues.json` after *every* venue. An aborted run keeps its created playlists
  and resolved tracks; the next run continues cheaply.
- **Playlist semantics live in `compose.py`.** Upcoming-only – a show stays in
  until **5pm local on its day** (`SAME_DAY_CUTOFF`), then drops. Ordered
  soonest-show-first; `build` then **round-robins tracks across artists** so acts
  are mixed, not stacked. If a venue has no upcoming shows the playlist is
  **renamed** (`"<venue> (no upcoming shows yet)"`) and its tracks are left
  intact – failures stay visible, never silently blank. Normal name is just the
  venue title.
- **Covers are cached, not hot-linked.** Spotify playlist image URLs expire
  ~24h, so `_cache_cover` downloads the auto-generated mosaic into
  `docs/assets/covers/<slug>.jpg` each run. (`ugc-image-upload` scope is reserved
  for future custom covers; not used yet.)
- **One internal shape.** `Event` is the only cross-module type; both data
  sources normalize into it, so `compose`/`build` never see source-specific JSON.

## Common tasks

**Run locally.** Put the 4–5 secrets in a gitignored `.env` (see Secrets), then:
```sh
uv run python -m venue_playlists.build      # then open docs/index.html
```

**Add a venue.** Find its Ticketmaster Discovery id (alphanumeric like
`KovZpZAAl7AA` – *not* the ticketmaster.com URL number), then add a block to
`config/venues.yaml` and run the build (it creates the playlist and records the
id):
```sh
curl -s "https://app.ticketmaster.com/discovery/v2/venues.json?keyword=THE%20NAME&stateCode=CA&apikey=$TICKETMASTER_API_KEY" \
  | python3 -c 'import sys,json;[print(v["id"],v["name"],v.get("city",{}).get("name")) for v in json.load(sys.stdin)["_embedded"]["venues"]]'
```
```yaml
      - name: Venue Name
        slug: venue-name-sf        # stable; names cover-art file + ids key
        source: ticketmaster
        ticketmaster_venue_id: KovZ...
        calendar_url: https://venue.example/   # shown/linked; required for scrape
```
For a venue **not** on Ticketmaster, use `source: scrape` with a `calendar_url`
and set `ANTHROPIC_API_KEY`. Prefer TM where it has coverage (cross-check the
venue's own calendar – TM can under-list rooms that ticket via See Tickets/DICE).

**Remove a venue.** Delete its block from `venues.yaml`, then unfollow its
playlist from the broadcasting account and prune the bookkeeping (build won't do
this for you):
```python
# uv run python - , with .env loaded
import json, httpx, os, pathlib
from venue_playlists.spotify import get_access_token
slug = "venue-name-sf"
ids = json.load(open("config/playlist_ids.json")); pid = ids.pop(slug, None)
tok = get_access_token(os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"], os.environ["SPOTIFY_REFRESH_TOKEN"])
httpx.delete(f"https://api.spotify.com/v1/playlists/{pid}/followers", headers={"Authorization": f"Bearer {tok}"})
json.dump(ids, open("config/playlist_ids.json","w"), indent=2, sort_keys=True)
pathlib.Path(f"docs/assets/covers/{slug}.jpg").unlink(missing_ok=True)
```
(`docs/data/venues.json` drops the venue on the next build.)

**Add a city.** Add a `cities:` entry with `name` + IANA `timezone`, then its
venues. The timezone drives the 5pm-local cutoff.

**Tune output.** `defaults` in `venues.yaml`: `tracks_per_artist` (3),
`max_tracks` (50 per playlist), `lookahead_days` (120). Per-venue overrides go on
the venue entry.

**Re-mint the Spotify token** (only if it breaks – see Gotchas): run
`scripts/mint_refresh_token.py` on a machine logged into the broadcasting account
and update the `SPOTIFY_REFRESH_TOKEN` secret.

## Secrets & accounts

Stored as repo **Actions secrets** (and locally in a gitignored `.env`):

| Secret | Purpose |
|---|---|
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | App credentials (Authorization Code flow). |
| `SPOTIFY_REFRESH_TOKEN` | Long-lived; the cron exchanges it for a 1h access token each run. |
| `TICKETMASTER_API_KEY` | Discovery API (the Consumer Key). |
| `ANTHROPIC_API_KEY` | Optional – only `source: scrape` venues need it (`claude-haiku-4-5`). |

The broadcasting Spotify account **must keep Spotify Premium** (dev-mode
requirement) and the app stays in **development mode** (no quota review needed).

## Gotchas

- **Spotify dev-mode (2026), the big ones:** no `top-tracks` / `recommendations`
  / audio-features / "This Is" playlists; page `limit` capped at **10** (50 →
  "Invalid limit"); `GET /albums/{id}/tracks` has a brutal per-endpoint quota
  (hammering it returns `Retry-After` ~24h while other endpoints stay 200) – the
  search-based assembly avoids it; `GET /playlists/{id}` no longer returns the
  `tracks` field, read counts/items via `GET /playlists/{id}/items`; create is
  `POST /me/playlists`, replace is `PUT /playlists/{id}/items` (≤100 uris),
  "delete" is unfollow (`DELETE /playlists/{id}/followers`).
- **Refresh-token durability:** it does not expire on a timer; it dies only if
  the account password changes, the app is revoked, or the requested scopes
  change. `SCOPES` in `spotify.py` already includes `ugc-image-upload` so adding
  custom covers later won't force a re-mint.
- **Pushing from this box needs SSH:** a global
  `url.https://github.com/.insteadOf git@github.com:` rewrite plus a
  workflow-scope-less `gh` token means HTTPS pushes of `.github/workflows/` are
  rejected. Use the `ssh://git@github.com/CatalinVoss/venue-playlists.git` remote
  form (already set on `origin`).
- **Cron is best-effort:** scheduled runs can be delayed/dropped at peak (hence
  the off-the-hour `06:17`), and a public-repo schedule auto-disables after 60
  days of no activity – the daily data commit keeps the clock reset.

## Conventions

Keep it small and boring. Match the existing style (no comments narrating
history; describe the final state). New cross-source data flows through `Event`.
Anything that can blow the Spotify quota goes through `SpotifyClient` so it's
throttled and backoff-capped. When you change behavior, update the matching line
above.
