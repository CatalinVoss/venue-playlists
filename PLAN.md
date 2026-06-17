# Venue Playlists – Plan

> Auto-maintained Spotify playlists, one per live-music venue, that capture each
> venue's booking taste **and** surface the artists playing there soon – so you
> discover a band this week and go catch their show on the weekend.

**Status:** plan for review. Nothing built or pushed yet. Origin: a voice memo
(passion project, separate from anything work-related). Provisional repo name:
`venue-playlists` (open to change).

---

## 0. TL;DR – key decisions baked in

- **One static page on GitHub Pages + one scheduled GitHub Action. $0 hosting.**
  No server, no real database – generated JSON and cached cover art are
  committed back into the repo by the Action, and the page reads them as files.
- **Data sources, per venue:** Ticketmaster Discovery API (free, instant key)
  where the venue is covered, with a **scrape-the-calendar + Claude-Haiku
  extraction** fallback for the venues Ticketmaster misses (most small indie
  rooms). LLM cost ≈ **$1–5/month**, near-$0 with skip-unchanged caching.
- **⚠️ The big one – Spotify killed the obvious track source.** As of Feb/Mar
  2026 a *new* Spotify app can **no longer** call `top-tracks`,
  `recommendations`, `audio-features`, or fetch the editorial "This Is {Artist}"
  playlists. Your memo assumed those. The supported replacement is to assemble
  each artist's tracks from **Search → Artist's Albums → Album Tracks** and rank
  them ourselves. Details in §3/§5. The end result is the same kind of playlist;
  the plumbing is just different.
- **Spotify dev-mode now requires the owner account to have Premium** and caps
  the app at 5 users (fine – we need 1). No app-review/quota process needed.
- **This doesn't already exist.** Closest is one abandoned 0-star CLI from 2022.
  Spotify's own "follow a venue" (Oct 2025) saves a venue's calendar but makes
  no playlist. The per-venue, auto-updated, soonest-show-first playlist is an
  open niche. (§14)

---

## 1. The product, precisely

A small set of cities. Per city, up to ~10 **medium-sized** venues (the rooms
where discovering a band and grabbing a ticket is realistic – The Independent,
not Chase Center). Each venue gets **one Spotify playlist** that:

1. Contains tracks from the artists with **upcoming** shows at that venue.
2. Is **ordered by show date** – the soonest show's artist is at the top, shows
   a few months out are at the bottom ("blend forward").
3. **Drops past shows.** A show counts as upcoming until **5:00 PM local on the
   day of the show** (so you can still catch it same-day), then it falls off.
4. **Fallback when the calendar is empty/stale:** if there are no upcoming
   shows, fall back to tracks from artists that *recently played* there, **and
   rename the playlist to flag it** (e.g. `The Independent – recent (no upcoming
   shows listed)`) so a stale/broken scrape is visible, never silent.
5. Has real **cover art** (Spotify's auto-generated mosaic of the album covers),
   cached into the repo so the landing page can show it.

The landing page is a single static page: a responsive grid of venue cards, each
showing the playlist's cover art + venue + city, click-through to follow on
Spotify. Looks sharp, works on phone and desktop, optional short "why I built
this" at the bottom.

---

## 2. Architecture

```
                    GitHub Actions (cron, daily, off-peak UTC)
                                    │
        ┌───────────────────────────┼────────────────────────────┐
        ▼                           ▼                             ▼
  EVENT INGEST                 SPOTIFY BUILD                 SITE DATA
  per venue:                   per artist:                  write /docs/data/
  • Ticketmaster API           • search → artist id         • venues.json
    (if covered)               • albums → album tracks      • per-venue art
  • else fetch calendar HTML   • rank → pick ~4 tracks         /docs/assets/covers/
    → clean (trafilatura)      per venue:                   commit + push back
    → Haiku extract            • PUT playlist items (≤100)        │
  → normalized events          • PUT name/description             ▼
    {artist,date,ticket_url}   • (cover = Spotify mosaic)   GitHub Pages serves
        │                                                   the static page,
        └──────── upcoming-only, sorted by date ───────────  reads venues.json
```

Everything runs inside one GitHub Action on a daily cron. The same job that
refreshes the playlists regenerates the site data and commits it – and that
commit doubles as the "repo activity" that keeps the scheduled workflow from
being auto-disabled after 60 days of inactivity.

---

## 3. Data sources

### 3a. Event data (who's playing, and when)

| Source | Verdict | Use |
|---|---|---|
| **Ticketmaster Discovery API** | Free, instant key, 5k calls/day. Covers *some* indie venues (The Independent SF = `venueId 229401`, Rickshaw Stop SF = `229795`). | **Primary** where the venue exists in TM. |
| **Scrape venue calendar + Haiku extract** | Every target venue publishes a static-HTML calendar. Robust to layout via LLM extraction. | **Fallback / primary** for venues TM misses (e.g. Bottom of the Hill, not in TM at all). |
| Bandsintown | Artist-centric only; can't list "who's at venue X"; key gated. | ✗ not used |
| Songkick | Public API closed to new keys; rejects hobby use. | ✗ not used |
| SeatGeek | New keys stuck in perpetual "pending"; weak indie coverage. | ✗ not used |

**Strategy:** a single Ticketmaster client + a generic "fetch HTML → clean →
Haiku → `{artist, date, time, ticket_url}` JSON" scraper as the universal
fallback. For each venue we configure which source to use (or try TM first, then
scrape to fill gaps). Honor robots.txt, polite User-Agent, cache by content hash.

### 3b. Spotify catalog access – the constraint that shapes everything

A **new** Spotify app (created now, in development mode) is blocked from:
`top-tracks`, `recommendations`, `audio-features`/`analysis`, `related-artists`,
featured/category playlists, **and reading Spotify-owned "This Is {Artist}"
playlists**. These are grandfathered only for apps that had extended access
before Nov 2024. There is no application path back for a hobby app.

**What still works for a new dev app:** Search, Get Artist, Get Artist's Albums,
Get Album Tracks, Get a single Track (popularity), all playlist write ops, cover
upload, Get Current User's Profile.

So we **cannot** "pull the tops or the This-Is playlist" as the memo suggested.
We assemble tracks ourselves (§5). Net effect on the listener is the same.

### 3c. Spotify write path (current 2026 endpoints)

- Create: `POST /v1/me/playlists` (the `/users/{id}/playlists` create path was removed)
- Replace tracks: `PUT /v1/playlists/{id}/items` (`/tracks` is deprecated; **≤100 URIs/request**)
- Rename/describe: `PUT /v1/playlists/{id}`
- Cover art: `PUT /v1/playlists/{id}/images` (base64 JPEG, **≤256 KB**) – only if we do custom covers
- Auth: Authorization Code flow, **refresh token minted once**, stored as a secret, exchanged for a 1-hour access token each run.

---

## 4. The daily refresh pipeline

1. **Refresh access token** – `POST /api/token` (grant_type=refresh_token). Immediately `::add-mask::` the access token so it can't leak into public logs.
2. **For each venue:** get upcoming events (TM API and/or scrape+extract). Normalize to `{artist, show_date, show_time, ticket_url}`. Filter to **upcoming** (date > today, or date == today and now < 17:00 local). Sort by date ascending.
3. **Resolve each artist → Spotify artist id** (Search, fuzzy-match guard to avoid mapping a common name / non-music booking to the wrong artist).
4. **Assemble ~4 tracks per artist** (§5).
5. **Build the ordered track list** for the venue: concatenate per-artist blocks in show-date order (soonest first → furthest out last). Cap to ≤100 tracks (Spotify replace limit; also keeps it listenable).
6. **Push to the playlist:** replace items, set name + description (description carries "updated {date} · {N} upcoming shows · source").
   - If **no upcoming** events: fall back to recently-played artists and set the **stale name** + a description that says the calendar may be broken.
7. **Cache cover art:** fetch `GET /v1/playlists/{id}` → `images[]`, download the 640×640, write to `/docs/assets/covers/{id}.jpg` (Spotify image URLs expire ~24h, so we must cache, not hot-link).
8. **Write `/docs/data/venues.json`** (the page's data) and **commit + push** everything back via the Action's `GITHUB_TOKEN`.

---

## 5. Playlist composition (given the Spotify limits)

Per artist with an upcoming show:

1. `GET /v1/search?q={name}&type=artist&limit=1` → artist id (validate the name match; skip on low confidence).
2. `GET /v1/artists/{id}/albums?include_groups=album,single&market=US` → newest first.
3. From the few most recent releases, `GET /v1/albums/{id}/tracks` → candidate tracks.
4. **Rank for "popular but not just the one hit":** look up per-track `popularity` (`GET /v1/tracks/{id}`) for a bounded candidate set, take the top ~4, dedupe by title (drop live/remaster dupes). If per-track popularity lookups are also restricted at build time, fall back to "a couple from the latest release + a couple from a mid release."
5. Keep it to ~3–5 tracks/artist so a 10–20 act calendar yields a ~50-track playlist, not a 300-track dump.

**Ordering = discovery mechanic.** Because per-artist blocks are concatenated in
show-date order, the top of every playlist is literally "who's playing soonest."

---

## 6. Cities & venues (initial)

Medium rooms only. Candidates (final list trimmed to ≤10/city):

- **SF:** The Independent, Rickshaw Stop, Bottom of the Hill *(⚠ announced it
  closes end of 2026 – keep but flagged)*, The Chapel, Cafe du Nord, Great
  American Music Hall, The Warfield(?). Your three are locked in.
- **NYC:** Bowery Ballroom, Music Hall of Williamsburg, Mercury Lounge,
  Elsewhere, Baby's All Right, Le Poisson Rouge.
- **LA:** The Troubadour, The Echo/Echoplex, Teragram Ballroom, Lodge Room, The
  Roxy, El Rey.

Ticketmaster coverage varies per venue; the scraper fallback fills the rest. Each
venue is one config entry: `{city, name, spotify_playlist_id, ticketmaster_venue_id?, calendar_url?, source}`.

---

## 7. Landing page

- **Single static `index.html`** on GitHub Pages – vanilla HTML/CSS, no React, no build step.
- Responsive card grid: `grid-template-columns: repeat(auto-fit, minmax(160px, 1fr))` – collapses to one column on phones with zero media queries.
- Each card = `<a href="https://open.spotify.com/playlist/{id}">` wrapping the cached cover image + venue name + city. Plain link is the right "follow" mechanism: it opens Spotify (app on mobile / web player on desktop) where the native Follow button lives. (Spotify's old JS "Follow" widget is dead; per-card iframe embeds would tank a 30-card grid.)
- "Pop" via CSS custom properties: one oversized display headline, a high-contrast/saturated palette, `transform: scale()` + shadow on card hover/focus. Optional short "why I built this" footer.
- Data-driven: the grid is rendered from `data/venues.json` (a tiny bit of vanilla JS, or pre-rendered at build time).

---

## 8. Repo layout

```
venue-playlists/
├── README.md
├── PLAN.md                      # this file
├── pyproject.toml               # uv-managed
├── config/venues.yaml           # the venue list (city, ids, calendar urls, source)
├── src/venue_playlists/
│   ├── spotify.py               # auth, search→albums→tracks, playlist writes, cover cache
│   ├── events/
│   │   ├── ticketmaster.py      # Discovery API client
│   │   └── scrape.py            # fetch + trafilatura clean + Haiku extract (structured output)
│   ├── compose.py               # upcoming filter, date sort, track selection, naming
│   └── build.py                 # orchestrates a full refresh + writes site data
├── scripts/
│   └── mint_refresh_token.py    # one-time local OAuth → prints REFRESH_TOKEN
├── docs/                        # GitHub Pages root
│   ├── index.html
│   ├── style.css
│   ├── data/venues.json         # generated
│   └── assets/covers/*.jpg      # generated, cached
└── .github/workflows/refresh.yml
```

## 9. Scheduling, secrets, safety

- `on: { schedule: [{cron: '17 6 * * *'}], workflow_dispatch: {} }` – off-the-hour minute to dodge Actions load spikes; manual trigger for testing. UTC, default branch.
- `permissions: { contents: write }`; commit generated data with the actions bot; the daily commit keeps the 60-day auto-disable clock reset.
- Secrets (repo Actions secrets): `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`, `TICKETMASTER_API_KEY`, `ANTHROPIC_API_KEY`.
- **Mask the computed Spotify access token** (`echo "::add-mask::$TOKEN"`) before any step could log it – GitHub only auto-masks `${{ secrets.* }}`, not runtime-computed tokens.
- Pages served from `/docs` on the default branch (simplest, $0, HTTPS).

## 10. Cost

- Hosting + Actions + "database": **$0** (public repo).
- LLM scraping: cleaned calendar page ≈ 1.5–4k input tokens; ~30 venues/day ≈ **$0.16/day (~$5/mo) worst case**, and **~$1/mo** with content-hash skip-unchanged (most calendars change weekly, not daily). Model: `claude-haiku-4-5` ($1/$5 per 1M).
- Spotify/Ticketmaster APIs: free.

## 11. What I need from you (the one config ping)

See the message accompanying this plan. In short: confirm the Spotify account has
Premium; create a Spotify app (I'll walk you through, ~2 min) and a free
Ticketmaster key; run one local script to mint the refresh token; provide an
Anthropic key (or defer the scraper); confirm the repo name. Then I build.

## 12. Build sequence

1. Repo scaffold + `config/venues.yaml` + the OAuth mint script (no secrets needed).
2. Spotify client (auth, track assembly, playlist writes, cover cache) – validate against your account once the refresh token exists.
3. Event ingest: Ticketmaster client + scraper, validated on the 3 SF venues.
4. `compose.py` + `build.py` end-to-end → real playlists appear on your account.
5. Landing page from real `venues.json`, checked on web + mobile (screenshots).
6. GitHub Action + secrets; verify a real scheduled/dispatch run is green.
7. Expand venues/cities; flip repo public + enable Pages on your approval.

## 13. Open risks

- **Spotify Premium requirement** for dev-mode API writes (need to confirm).
- **Artist-name → Spotify resolution** is lossy (tribute acts, DJs, common names) – needs a confidence guard.
- **JS-rendered venue calendars** would defeat raw-HTML scraping (would need a headless fetch). The three SF targets are static; we check each new venue.
- **Spotify could ship this itself** (venue-following landed Oct 2025) – acceptable for a passion project.

## 14. Prior art / novelty

Saturated: artist-following concert recs and city-wide "concerts near you"
playlists (incl. Spotify's own). **Not built:** one auto-maintained playlist *per
venue*, ordered by soonest show. Closest is `cdibble/spotlive` (abandoned CLI,
0 stars, 2022, one combined playlist, no scheduling, no date order). Spotify's
Oct-2025 "follow a venue" saves a calendar to your library but generates no
playlist. So the specific idea is genuinely open.
