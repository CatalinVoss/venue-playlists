# Venue Playlists

### ▶ Live: https://catalinvoss.github.io/venue-playlists/

A Spotify playlist for every live-music venue in your city – the artists playing
there soon, **soonest show first**. Discover a band at the top of the list, then
go catch their set this week.

A single static page on GitHub Pages, refreshed every day by a GitHub Action. No
server, no database – generated data is committed back into the repo. Runs for
~$0 (just a few dollars a month of Claude usage for the calendar scraper, often
less).

Maintaining this? See [AGENTS.md](AGENTS.md).

## How it works

```
GitHub Action (daily cron)
  ├─ read each venue's calendar      (Ticketmaster API, or scrape + Claude Haiku)
  ├─ keep upcoming shows, soonest first
  ├─ build a playlist per venue      (Search → Artist's Albums → Album Tracks)
  └─ commit cover art + venues.json  → GitHub Pages serves the page
```

- **Event data:** Ticketmaster Discovery API where a venue is covered, else the
  venue's own calendar page is scraped and cleaned, then a cheap Claude Haiku
  call extracts `{artist, date, ticket_url}`. Unchanged pages are skipped.
- **Track selection:** a *new* Spotify app can no longer use top-tracks,
  recommendations, or "This Is" playlists, so tracks are assembled from each
  artist's albums and ranked by per-track popularity.
- **Ordering:** per-artist track blocks are concatenated in show-date order, so
  the soonest show is at the top of the playlist.
- **Failure is visible:** if a venue has no upcoming shows (e.g. a broken
  scrape), the playlist is renamed to flag it instead of silently emptying.

## Layout

- `config/venues.yaml` – the venue list (city, source, calendar URL, TM id)
- `venue_playlists/` – the Python package (Spotify, event ingest, compose, build)
- `scripts/mint_refresh_token.py` – one-time Spotify OAuth helper
- `docs/` – the GitHub Pages site (`index.html`, `style.css`, generated `data/`)
- `.github/workflows/refresh.yml` – the daily refresh job

## Setup

1. **Spotify app** – create one at the [developer dashboard](https://developer.spotify.com/dashboard)
   with redirect URI `http://127.0.0.1:8888/callback`. The owning account needs
   Spotify Premium. Copy the Client ID + Secret.
2. **Mint a refresh token** –
   ```sh
   SPOTIFY_CLIENT_ID=… SPOTIFY_CLIENT_SECRET=… uv run python scripts/mint_refresh_token.py
   ```
3. **Ticketmaster key** – free, instant at [developer.ticketmaster.com](https://developer.ticketmaster.com).
4. **Anthropic key** – for the calendar scraper.
5. Add all five as repo **Actions secrets**: `SPOTIFY_CLIENT_ID`,
   `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`, `TICKETMASTER_API_KEY`,
   `ANTHROPIC_API_KEY`.
6. Run it: `uv run python -m venue_playlists.build` (or trigger the workflow).

## Run locally

Put the secrets in a local `.env` (gitignored) and:

```sh
uv run python -m venue_playlists.build
```

Then open `docs/index.html`.
