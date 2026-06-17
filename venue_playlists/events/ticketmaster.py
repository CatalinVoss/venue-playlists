"""Ticketmaster Discovery API v2 client (free tier).

Docs: https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import httpx
from dateutil import parser as dateparser

from venue_playlists.models import Event

logger = logging.getLogger(__name__)

_BASE = "https://app.ticketmaster.com/discovery/v2"


def upcoming_events(
    api_key: str,
    venue_id: str,
    *,
    lookahead_days: int = 120,
    client: httpx.Client | None = None,
) -> list[Event]:
    """Return upcoming music events for a Ticketmaster venue id.

    Returns headliner names with the show date and the Ticketmaster ticket URL.
    Empty list (not an exception) on no results so the caller can fall back.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=20)
    params = {
        "apikey": api_key,
        "venueId": venue_id,
        "classificationName": "music",
        "sort": "date,asc",
        "size": "100",
    }
    events: list[Event] = []
    try:
        resp = client.get(f"{_BASE}/events.json", params=params)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:  # network / 4xx / 5xx
        logger.warning("Ticketmaster request failed for venue %s: %s", venue_id, exc)
        return []
    finally:
        if owns_client:
            client.close()

    for ev in payload.get("_embedded", {}).get("events", []):
        parsed = _parse_event(ev)
        if parsed is not None:
            events.append(parsed)
    logger.info("Ticketmaster: %d events for venue %s", len(events), venue_id)
    return events


def _parse_event(ev: dict) -> Event | None:
    name = (ev.get("name") or "").strip()
    if not name:
        return None

    local_date = (ev.get("dates", {}).get("start", {}) or {}).get("localDate")
    if not local_date:
        return None
    try:
        show_date = dateparser.parse(local_date).date()
    except (ValueError, TypeError):
        return None

    local_time = (ev.get("dates", {}).get("start", {}) or {}).get("localTime")

    # Prefer the named attractions (the actual artists) over the marketing event
    # title, which is often "Artist - with Support".
    attractions = (ev.get("_embedded", {}) or {}).get("attractions") or []
    headliner = attractions[0].get("name").strip() if attractions else name
    support = [a.get("name", "").strip() for a in attractions[1:] if a.get("name")]

    return Event(
        artist=headliner,
        show_date=show_date,
        show_time=local_time,
        support_acts=support,
        ticket_url=ev.get("url"),
    )
