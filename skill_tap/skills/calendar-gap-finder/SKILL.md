---
name: calendar-gap-finder
description: Find meeting windows within working hours from an inclusive date range, a duration, and calendar busy intervals. Handles overlaps, all-day blocks, and timezones.
---

# Calendar gap finder

Use `a3_gap_finder` if the assistant provides it. In a standalone Hermes install, run `python3 scripts/find_gaps.py` from this skill's directory with the input JSON on stdin. This script needs only Python and timezone data.

Supply `start_date` and `end_date` as inclusive ISO dates, `duration_minutes` as an integer, and `busy` as a list of `{ "start": "…", "end": "…" }` objects. Use RFC3339 timestamps with UTC offsets or ISO dates for all-day intervals. All ends are exclusive. Optional `timezone`, `working_hours` (`start` and `end` as same-day `HH:MM`), and `weekdays` (Monday `0` through Sunday `6`) default to Los Angeles, 09:00–17:00, and weekdays.

Report the returned `available_windows` in order. Each window can accommodate the full duration. If the list is empty, ask the user for a wider date range or shorter duration.
