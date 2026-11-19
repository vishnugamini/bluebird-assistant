"""Load the exact script shipped in the public skill, avoiding a second implementation."""
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo

_path = Path(__file__).resolve().parent.parent / "skill_tap/skills/calendar-gap-finder/scripts/find_gaps.py"
_spec = importlib.util.spec_from_file_location("calendar_gap_finder", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
find_gaps = _module.find_gaps


def conflicts(items, timezone):
    zone = ZoneInfo(timezone)
    ranges = []
    for item in items:
        start = item["start"].get("dateTime") or item["start"]["date"]
        end = item["end"].get("dateTime") or item["end"]["date"]
        ranges.append((_module.instant(start, zone), _module.instant(end, zone), item["id"]))
    active, result = [], []
    for start, end, event_id in sorted(ranges):
        active = [(stop, other) for stop, other in active if stop > start]
        result.extend([other, event_id] for _, other in active)
        active.append((end, event_id))
    return result
