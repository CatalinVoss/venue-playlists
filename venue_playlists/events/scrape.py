"""Generic venue-calendar scraper: fetch HTML -> clean -> Claude Haiku extract.

Most small venues are not in any free event API, but they all publish a static
HTML calendar. We clean the page to markdown (trafilatura) to cut tokens, then
make one cheap Claude Haiku call per page with a strict JSON schema. A content
hash skips the model call entirely when a page hasn't changed since last run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import trafilatura
from dateutil import parser as dateparser

from venue_playlists.models import Event

logger = logging.getLogger(__name__)

# Cheapest Claude tier; ample for HTML -> structured extraction.
MODEL = "claude-haiku-4-5"

# Trim absurdly long pages before sending. A calendar rarely needs more.
MAX_INPUT_CHARS = 24_000

_EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "artist": {"type": "string"},
                    "date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                    "time": {"type": "string"},
                    "support_acts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["artist", "date", "time", "support_acts"],
            },
        }
    },
    "required": ["events"],
}


def fetch_clean(url: str) -> str | None:
    """Download `url` and return cleaned markdown, or None on failure."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        logger.warning("scrape: could not fetch %s", url)
        return None
    text = trafilatura.extract(
        downloaded,
        output_format="markdown",
        include_links=True,
        favor_recall=True,
    )
    if not text:
        logger.warning("scrape: nothing extracted from %s", url)
        return None
    return text[:MAX_INPUT_CHARS]


def extract_events(anthropic_client, text: str, today: date) -> list[Event]:
    """One Haiku call: cleaned page text -> structured upcoming events."""
    prompt = (
        f"Today is {today.isoformat()}. The text below is a live-music venue's "
        "event calendar. Extract every UPCOMING show as structured data.\n\n"
        "Rules:\n"
        "- Resolve each date to a full ISO date (YYYY-MM-DD). The calendar may "
        "omit the year; infer it so the date is today or in the future (never "
        "in the past).\n"
        "- `artist` is the headliner only. Put other listed acts in "
        "`support_acts`.\n"
        "- Skip non-music bookings (comedy, private events, trivia) and any row "
        "whose date you cannot resolve.\n"
        "- If there are no upcoming shows, return an empty events list.\n\n"
        f"CALENDAR:\n{text}"
    )
    resp = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": _EVENT_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        logger.warning("scrape: model refused extraction")
        return []
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("scrape: model returned non-JSON")
        return []
    return _to_events(data.get("events", []))


def _to_events(rows: list[dict]) -> list[Event]:
    out: list[Event] = []
    for row in rows:
        artist = (row.get("artist") or "").strip()
        raw_date = row.get("date")
        if not artist or not raw_date:
            continue
        try:
            show_date = dateparser.parse(raw_date).date()
        except (ValueError, TypeError):
            continue
        out.append(
            Event(
                artist=artist,
                show_date=show_date,
                show_time=(row.get("time") or "").strip() or None,
                support_acts=[s.strip() for s in row.get("support_acts", []) if s.strip()],
            )
        )
    return out


# --- content-hash cache so unchanged pages cost nothing -------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ScrapeCache:
    """Maps venue slug -> {hash, events}. Persisted as JSON in the repo."""

    path: Path
    data: dict

    @classmethod
    def load(cls, path: Path) -> "ScrapeCache":
        if path.exists():
            return cls(path, json.loads(path.read_text()))
        return cls(path, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def get(self, slug: str, text: str) -> list[Event] | None:
        entry = self.data.get(slug)
        if entry and entry.get("hash") == _hash(text):
            logger.info("scrape: %s unchanged, using cached events", slug)
            return _to_events(entry.get("events", []))
        return None

    def put(self, slug: str, text: str, events: list[Event]) -> None:
        self.data[slug] = {"hash": _hash(text), "events": [e.as_dict() for e in events]}


def scrape_events(
    url: str,
    slug: str,
    anthropic_client,
    today: date,
    cache: ScrapeCache,
) -> list[Event]:
    """Fetch + extract a venue calendar, skipping the model call if unchanged."""
    text = fetch_clean(url)
    if text is None:
        return []
    cached = cache.get(slug, text)
    if cached is not None:
        return cached
    events = extract_events(anthropic_client, text, today)
    cache.put(slug, text, events)
    return events
