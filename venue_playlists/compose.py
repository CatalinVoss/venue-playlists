"""Turn a venue's raw events into an ordered, upcoming-only show list and the
playlist's name/description.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from venue_playlists.models import Event

# A show stays "upcoming" until this local time on the day of the show, so you
# can still decide to go that evening.
SAME_DAY_CUTOFF = time(17, 0)


def upcoming_events(events: list[Event], tz_name: str, *, lookahead_days: int) -> list[Event]:
    """Keep only future shows (today counts until 5pm local), sorted soonest-first."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    today = now.date()
    horizon = today.toordinal() + lookahead_days

    kept: list[Event] = []
    for ev in events:
        if ev.show_date > today:
            if ev.show_date.toordinal() <= horizon:
                kept.append(ev)
        elif ev.show_date == today and now.time() < SAME_DAY_CUTOFF:
            kept.append(ev)
        # past shows are dropped
    kept.sort(key=lambda e: (e.show_date, e.show_time or ""))
    return kept


def playlist_name(venue_name: str, *, stale: bool) -> str:
    if stale:
        return f"{venue_name} – recent (no upcoming shows listed)"
    return f"{venue_name} – upcoming shows"


def playlist_description(venue_name: str, *, n_shows: int, updated, stale: bool) -> str:
    when = updated.strftime("%b %-d, %Y")
    if stale:
        return (
            f"No upcoming shows found for {venue_name} (calendar may be stale). "
            f"Showing artists who recently played. Updated {when}. "
            "Auto-generated · venue-playlists."
        )
    return (
        f"Artists with upcoming shows at {venue_name}, soonest first. "
        f"{n_shows} shows · updated {when}. Auto-generated · venue-playlists."
    )
