"""
Provider schedules, slot finder, and date-formatting helpers.

Day-of-week mapping:
  Node.js getDay():  0=Sun 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri 6=Sat
  Python weekday():  0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun

  Conversion: python_weekday = (js_getday - 1) % 7
"""
import math
import random
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from .models import Slot

# ── Appointment type metadata ─────────────────────────────────────────────────

APPT_DURATIONS: dict[str, int] = {
    "new_patient": 60,
    "follow_up": 30,
    "urgent_follow_up": 30,
    "stress_test": 90,
    "np_intake": 45,
}

PROVIDER_APPT_TYPES: dict[str, list[str]] = {
    "Dr. Sarah Chen":    ["new_patient", "follow_up", "urgent_follow_up", "stress_test"],
    "Dr. Marcus Webb":   ["new_patient", "follow_up", "urgent_follow_up", "stress_test"],
    "Jennifer Park, NP": ["np_intake"],
}

# Keyed by Python weekday() (0=Mon … 4=Fri)
PROVIDER_SCHEDULE: dict[str, dict[str, list[int]]] = {
    "Dr. Sarah Chen":    {"SF": [0, 2, 4], "Oakland": [1, 3]},   # Mon/Wed/Fri, Tue/Thu
    "Dr. Marcus Webb":   {"SF": [1, 3]},                          # Tue/Thu
    "Jennifer Park, NP": {"SF": [0, 1, 2, 3, 4]},                # Mon–Fri
}

LOCATION_ADDRESSES: dict[str, str] = {
    "SF":      "450 Market Street, Suite 300, San Francisco, CA 94105",
    "Oakland": "2800 Broadway, Suite 110, Oakland, CA 94611",
}

# ── Provider / location normalizers ──────────────────────────────────────────

import re

_PROVIDER_ALIASES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"chen",                    re.IGNORECASE), "Dr. Sarah Chen"),
    (re.compile(r"webb",                    re.IGNORECASE), "Dr. Marcus Webb"),
    (re.compile(r"jennifer|park|np|nurse",  re.IGNORECASE), "Jennifer Park, NP"),
]


def normalize_provider(raw: str) -> Optional[str]:
    for pattern, canonical in _PROVIDER_ALIASES:
        if pattern.search(raw):
            return canonical
    if raw in PROVIDER_SCHEDULE:
        return raw
    return None


def normalize_location(raw: str) -> Optional[str]:
    lower = raw.lower().strip()
    if lower == "sf" or "san francisco" in lower or "market" in lower:
        return "SF"
    if lower == "oakland" or "broadway" in lower:
        return "Oakland"
    return None


# ── Slot helpers ──────────────────────────────────────────────────────────────

def slots_needed(appt_type: str) -> int:
    return math.ceil(APPT_DURATIONS.get(appt_type, 30) / 30)


def find_available_blocks(
    db: Session,
    provider: str,
    location: str,
    appt_type: str,
    preferred_date: Optional[str] = None,
) -> list[dict]:
    """
    Return consecutive open-slot blocks that fit appt_type duration.
    preferred_date, if provided, should be a YYYY-MM-DD string.
    """
    needed = slots_needed(appt_type)

    query = (
        db.query(Slot)
        .filter(
            Slot.provider == provider,
            Slot.location == location,
            Slot.status == "open",
        )
    )

    if preferred_date:
        # ISO strings are lexicographically sortable; "YYYY-MM-DD" prefix works.
        query = query.filter(Slot.start_iso >= preferred_date)

    rows = query.order_by(Slot.start_iso).all()

    blocks: list[dict] = []
    for i in range(len(rows) - needed + 1):
        ok = True
        for j in range(1, needed):
            if rows[i + j - 1].end_iso != rows[i + j].start_iso:
                ok = False
                break
        if ok:
            blocks.append({
                "start_iso": rows[i].start_iso,
                "end_iso":   rows[i + needed - 1].end_iso,
                "slot_ids":  [rows[i + k].id for k in range(needed)],
            })

    return blocks


# ── Date formatting ───────────────────────────────────────────────────────────

_DAY_NAMES = [
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
]
_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _ordinal(n: int) -> str:
    if n % 100 in (11, 12, 13):
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd'][n % 10] if n % 10 <= 3 else 'th'}"


def format_spoken_datetime(iso_string: str) -> str:
    """
    Convert an ISO 8601 UTC string to a spoken-English date/time phrase, e.g.:
      "Monday January 20th at 9am"
    Uses UTC to match server behaviour (same as Node when deployed in UTC).
    """
    d = datetime.fromisoformat(iso_string.replace("Z", "+00:00")).astimezone(timezone.utc)

    h, m = d.hour, d.minute
    ampm = "pm" if h >= 12 else "am"
    h12 = h % 12 or 12
    min_str = "" if m == 0 else f":{m:02d}"

    # Convert Python weekday (0=Mon) → JS-style index (0=Sun) for DAY_NAMES
    js_dow = (d.weekday() + 1) % 7

    return (
        f"{_DAY_NAMES[js_dow]} {_MONTH_NAMES[d.month - 1]} "
        f"{_ordinal(d.day)} at {h12}{min_str}{ampm}"
    )


# ── Confirmation ID ───────────────────────────────────────────────────────────

_CONF_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_confirmation_id() -> str:
    return "GC-" + "".join(random.choices(_CONF_CHARS, k=6))


# ── Fallback start-time calculator ───────────────────────────────────────────

# Provider start hours by location (UTC, matching the seed schedule)
_PROVIDER_START_HOURS: dict[str, dict[str, int]] = {
    "Dr. Sarah Chen":    {"SF": 9,  "Oakland": 10},
    "Dr. Marcus Webb":   {"SF": 8},
    "Jennifer Park, NP": {"SF": 8},
}


def next_scheduled_start(provider: str, location: str) -> str:
    """
    Return the ISO 8601 UTC string of the next valid start slot for this
    provider/location based on their schedule, scanning up to 14 days ahead.
    Falls back to utcnow() if no match found.
    """
    schedule = PROVIDER_SCHEDULE.get(provider, {}).get(location)
    if not schedule:
        return datetime.now(timezone.utc).isoformat()

    start_hour = _PROVIDER_START_HOURS.get(provider, {}).get(location, 9)
    now = datetime.now(timezone.utc)

    for offset in range(14):
        candidate = (now + timedelta(days=offset)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
        if candidate.weekday() in schedule and candidate > now:
            return candidate.isoformat()

    return now.isoformat()
