# Calendar Gap Finder for Hermes

Add this public repository as a skill tap, then install its calendar gap finder.

```sh
hermes skills tap add vishnugamini/bluebird-calendar-skills
hermes skills tap list
hermes skills install vishnugamini/bluebird-calendar-skills/skills/calendar-gap-finder
```

From the repository, run the standalone script with synthetic input.

```sh
python3 skills/calendar-gap-finder/scripts/find_gaps.py <<'JSON'
{
  "start_date": "2026-11-16",
  "end_date": "2026-11-16",
  "duration_minutes": 45,
  "timezone": "America/Los_Angeles",
  "busy": [{"start": "2026-11-16T10:00:00-08:00", "end": "2026-11-16T11:30:00-08:00"}]
}
JSON
```

The result has two windows, 09:00–10:00 and 11:30–17:00 Pacific. Run `python3 -m unittest discover -s tests` to verify the standalone skill. It uses Python `datetime` and `zoneinfo`, with no external API or credentials.

The implementation is original, with AI-assisted development recorded in the Assignment 3 process log. The tap follows the [Hermes skill publishing guide](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills).
