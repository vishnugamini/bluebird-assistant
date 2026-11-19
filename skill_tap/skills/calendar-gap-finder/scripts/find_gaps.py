#!/usr/bin/env python3
"""Find available meeting starts using timezone-aware, half-open intervals."""
from datetime import date, datetime, time, timedelta, timezone
import json
import sys
from zoneinfo import ZoneInfo


def instant(value, zone):
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time(), zone).astimezone(timezone.utc)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Event date-times must include a UTC offset. All-day events use dates.")
    return result.astimezone(timezone.utc)


def wall(day, clock, zone):
    naive = datetime.combine(day, time.fromisoformat(clock))
    local = naive.replace(tzinfo=zone, fold=0)
    if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError("Working hours contain a nonexistent daylight-saving time.")
    return local.astimezone(timezone.utc)


def find_gaps(data):
    zone = ZoneInfo(data.get("timezone", "America/Los_Angeles"))
    first = date.fromisoformat(data["start_date"])
    last = date.fromisoformat(data["end_date"])
    minutes = data["duration_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
        raise ValueError("duration_minutes must be an integer between 1 and 1440.")
    if not 0 <= (last - first).days <= 366:
        raise ValueError("Use an inclusive date range of at most 367 days.")
    hours = data.get("working_hours", {"start": "09:00", "end": "17:00"})
    weekdays = data.get("weekdays", [0, 1, 2, 3, 4])
    if any(isinstance(v, bool) or not isinstance(v, int) or v not in range(7) for v in weekdays):
        raise ValueError("weekdays uses Monday=0 through Sunday=6.")
    busy = []
    for event in data.get("busy", []):
        start, end = instant(event["start"], zone), instant(event["end"], zone)
        if end <= start:
            raise ValueError("Each busy interval must end after its start. All-day ends are exclusive.")
        busy.append((start, end))
    slots = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        start, end = wall(day, hours["start"], zone), wall(day, hours["end"], zone)
        if end <= start:
            raise ValueError("Working hours must end after they start on the same date.")
        cursor = start
        for lower, upper in sorted(busy):
            if upper <= cursor or lower >= end:
                continue
            available_end = min(lower, end)
            if available_end - cursor >= timedelta(minutes=minutes):
                slots.append({"start": cursor.astimezone(zone).isoformat(),
                              "end": available_end.astimezone(zone).isoformat()})
            cursor = max(cursor, upper)
            if cursor >= end:
                break
        if end - cursor >= timedelta(minutes=minutes):
            slots.append({"start": cursor.astimezone(zone).isoformat(), "end": end.astimezone(zone).isoformat()})
    return {"timezone": str(zone), "duration_minutes": minutes, "available_windows": slots}


if __name__ == "__main__":
    try:
        print(json.dumps(find_gaps(json.load(sys.stdin)), indent=2))
    except (ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(2)
