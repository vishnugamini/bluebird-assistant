import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[1] / "skills/calendar-gap-finder/scripts/find_gaps.py"
spec = importlib.util.spec_from_file_location("find_gaps", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GapsTest(unittest.TestCase):
    def test_overlap_and_adjacent_intervals(self):
        result = module.find_gaps({"start_date": "2026-11-16", "end_date": "2026-11-16", "duration_minutes": 60,
                                  "busy": [{"start": "2026-11-16T18:00:00Z", "end": "2026-11-16T19:00:00Z"},
                                           {"start": "2026-11-16T10:30:00-08:00", "end": "2026-11-16T13:00:00-08:00"}]})
        self.assertEqual([row["start"][11:16] for row in result["available_windows"]], ["09:00", "13:00"])

    def test_all_day_and_weekend(self):
        result = module.find_gaps({"start_date": "2026-11-13", "end_date": "2026-11-16", "duration_minutes": 30,
                                  "busy": [{"start": "2026-11-13", "end": "2026-11-17"}]})
        self.assertEqual(result["available_windows"], [])

    def test_fall_back_uses_elapsed_time(self):
        result = module.find_gaps({"start_date": "2026-11-01", "end_date": "2026-11-01", "duration_minutes": 180,
                                  "weekdays": [6], "working_hours": {"start": "00:00", "end": "02:00"}})
        self.assertEqual(len(result["available_windows"]), 1)
        self.assertTrue(result["available_windows"][0]["start"].endswith("-07:00"))
        self.assertTrue(result["available_windows"][0]["end"].endswith("-08:00"))

    def test_spring_forward_rejects_nonexistent_working_time(self):
        with self.assertRaisesRegex(ValueError, "nonexistent"):
            module.find_gaps({"start_date": "2026-03-08", "end_date": "2026-03-08", "duration_minutes": 30,
                              "weekdays": [6], "working_hours": {"start": "02:30", "end": "04:00"}})
