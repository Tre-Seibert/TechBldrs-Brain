"""Dates, durations and hours as short text for tool replies. Source-agnostic."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _span(delta: Any) -> str:
    """1d 1h, 3h 20m, 12m: how long a timedelta is, for 'overdue by' and 'ago'."""
    seconds = max(int(delta.total_seconds()), 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _stamp(moment: datetime, *, with_time: bool) -> str:
    text = f"{moment:%b} {moment.day}"
    if with_time:
        text += f", {int(moment.strftime('%I'))}:{moment:%M} {moment:%p}"
    return text


def _due_text(value: Any, now: datetime) -> str:
    due = _when(value)
    if due is None:
        return "no due date"
    delta = now - due
    state = f"{_span(delta)} overdue" if delta.total_seconds() > 0 else f"due in {_span(-delta)}"
    return f"due {_stamp(due, with_time=True)} ({state})"


def _ago(moment: datetime, now: datetime) -> str:
    days = (now.date() - moment.date()).days  # calendar days, so it agrees with the date shown
    delta = now - moment
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if delta.total_seconds() >= 3600:
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return "just now"


def _created_text(value: Any, now: datetime) -> str:
    created = _when(value)
    if created is None:
        return "created date unknown"
    return f"created {_stamp(created, with_time=False)} ({_ago(created, now)})"


def _activity_text(value: Any, now: datetime) -> str:
    last = _when(value)
    if last is None:
        return "no activity recorded"
    days = (now.date() - last.date()).days  # calendar days, so it agrees with the date shown
    delta = now - last
    if days >= 1:
        ago = f"{days} day{'s' if days != 1 else ''} ago"
    elif delta.total_seconds() >= 3600:
        ago = f"{int(delta.total_seconds() // 3600)}h ago"
    else:
        ago = "just now"
    return f"last activity {_stamp(last, with_time=False)} ({ago})"


def _format_reached_at(value: datetime) -> str:
    hour = value.hour % 12 or 12
    ampm = "AM" if value.hour < 12 else "PM"
    return f"{value.strftime('%b')} {value.day}, {value.year} {hour}:{value.minute:02d} {ampm}"


def _format_hours(hours: float) -> str:
    text = f"{hours:.2f}".rstrip("0").rstrip(".")
    return f"{text} hour" if text == "1" else f"{text} hours"


def _format_minutes(minutes: int) -> str:
    """45m, 2h, 2h 05m: hours and minutes, never a bare minute count."""
    hours, mins = divmod(int(minutes), 60)
    if not hours:
        return f"{mins}m"
    return f"{hours}h" if not mins else f"{hours}h {mins:02d}m"
