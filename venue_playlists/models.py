"""Shared data shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class Event:
    """One upcoming show at a venue."""

    artist: str
    show_date: date
    show_time: str | None = None  # free-form local time, e.g. "8:00 PM"
    support_acts: list[str] = field(default_factory=list)
    ticket_url: str | None = None

    def as_dict(self) -> dict:
        return {
            "artist": self.artist,
            "show_date": self.show_date.isoformat(),
            "show_time": self.show_time,
            "support_acts": self.support_acts,
            "ticket_url": self.ticket_url,
        }
