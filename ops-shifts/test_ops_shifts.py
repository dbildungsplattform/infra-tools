import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure ops_shifts module is importable regardless of working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_shifts import OpsShiftsManager


class TestOpsShifts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        self.config_content = """
confluence:
  base_url: "https://docs.dbildungscloud.de"
  calendar_id: "test-calendar-id"
  calendar_name: "SRE"
  calendar_page_url: "https://docs.dbildungscloud.de/test"

teams:
  channel_name: "SRE Channel"
  runbook_url: "https://docs.dbildungscloud.de/runbooks"

rotation:
  timezone: "Europe/Berlin"
  shift_duration_days: 7
  shift_start_weekday: 0
  shift_start_hour: 9
  vacation_overlap_threshold_days: 1

team:
  - name: "Alice Developer"
    aliases: ["Alice", "adeveloper"]
    teams_email: "alice@dbildungscloud.de"
    active: true
  - name: "Bob Sysadmin"
    aliases: ["Bob", "bsysadmin"]
    teams_email: "bob@dbildungscloud.de"
    active: true
  - name: "Charlie SRE"
    aliases: ["Charlie", "csre"]
    teams_email: "charlie@dbildungscloud.de"
    active: true
"""
        self.config_path = self.temp_path / "config.yaml"
        self.config_path.write_text(self.config_content, encoding="utf-8")
        self.state_path = self.temp_path / "state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_member_on_vacation(self):
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
        )

        member = {"name": "Alice Developer", "aliases": ["Alice", "adeveloper"]}
        events = [
            {
                "summary": "Alice - Urlaub / Vacation",
                "start": dt.date(2026, 11, 2),
                "end": dt.date(2026, 11, 6),
            },
            {
                "summary": "Team Offsite",
                "start": dt.date(2026, 11, 10),
                "end": dt.date(2026, 11, 12),
            },
        ]

        # Shift during Alice's vacation
        on_vac, evs = manager.is_member_on_vacation(
            member, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9), threshold_days=1
        )
        self.assertTrue(on_vac)
        self.assertEqual(len(evs), 1)

        # Shift after Alice's vacation
        on_vac_after, _ = manager.is_member_on_vacation(
            member, events, dt.date(2026, 11, 16), dt.date(2026, 11, 23), threshold_days=1
        )
        self.assertFalse(on_vac_after)

    def test_fair_rotation_least_shifts(self):
        """Verify candidate with fewest shifts is picked first."""
        initial_state = {
            "last_updated": "2026-11-01T00:00:00+01:00",
            "current_shift": None,
            "member_stats": {
                "Alice Developer": {"shifts_completed": 3, "last_shift_end": "2026-10-15"},
                "Bob Sysadmin": {"shifts_completed": 1, "last_shift_end": "2026-10-01"},
                "Charlie SRE": {"shifts_completed": 2, "last_shift_end": "2026-10-08"},
            },
            "history": [],
        }
        self.state_path.write_text(json.dumps(initial_state), encoding="utf-8")

        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 2, 9, 30),
        )

        candidates = manager.config["team"]
        selected, _ = manager.pick_fair_candidate(
            candidates, [], dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        # Bob has 1 shift, lowest of all
        self.assertEqual(selected["name"], "Bob Sysadmin")

    def test_fair_rotation_tie_breaker(self):
        """Verify tie is broken by longest time since last shift."""
        initial_state = {
            "last_updated": "2026-11-01T00:00:00+01:00",
            "current_shift": None,
            "member_stats": {
                "Alice Developer": {"shifts_completed": 2, "last_shift_end": "2026-09-01"},
                "Bob Sysadmin": {"shifts_completed": 2, "last_shift_end": "2026-10-01"},
                "Charlie SRE": {"shifts_completed": 3, "last_shift_end": "2026-10-15"},
            },
            "history": [],
        }
        self.state_path.write_text(json.dumps(initial_state), encoding="utf-8")

        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 2, 9, 30),
        )

        candidates = manager.config["team"]
        selected, _ = manager.pick_fair_candidate(
            candidates, [], dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        # Alice and Bob both have 2 shifts, but Alice's last shift ended Sept 1 (older than Oct 1)
        self.assertEqual(selected["name"], "Alice Developer")

    def test_vacation_skips_eligible_candidate(self):
        """If least-shifts candidate is on vacation, skip to next eligible."""
        initial_state = {
            "last_updated": "2026-11-01T00:00:00+01:00",
            "current_shift": None,
            "member_stats": {
                "Alice Developer": {"shifts_completed": 1, "last_shift_end": "2026-09-01"},
                "Bob Sysadmin": {"shifts_completed": 2, "last_shift_end": "2026-10-01"},
                "Charlie SRE": {"shifts_completed": 3, "last_shift_end": "2026-10-15"},
            },
            "history": [],
        }
        self.state_path.write_text(json.dumps(initial_state), encoding="utf-8")

        events = [
            {
                "summary": "Alice Developer Urlaub",
                "start": dt.date(2026, 11, 2),
                "end": dt.date(2026, 11, 8),
            }
        ]

        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 2, 9, 30),
        )

        candidates = manager.config["team"]
        selected, _ = manager.pick_fair_candidate(
            candidates, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        # Alice has lowest shifts (1) but is on vacation -> Bob (2) is chosen!
        self.assertEqual(selected["name"], "Bob Sysadmin")

    def test_state_saving_and_advance(self):
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=False,
            mock_calendar=True,
            simulated_now=dt.datetime(2026, 11, 2, 9, 30),
        )

        current, next_prev, is_handoff, _ = manager.resolve_shifts()
        self.assertTrue(is_handoff)
        self.assertIsNotNone(current)
        self.assertIsNotNone(next_prev)
        self.assertTrue(self.state_path.exists())

        # Read back saved state
        with open(self.state_path, "r", encoding="utf-8") as f:
            saved_state = json.load(f)

        self.assertEqual(saved_state["current_shift"]["engineer"], current["engineer"])
        self.assertEqual(saved_state["member_stats"][current["engineer"]]["shifts_completed"], 1)


if __name__ == "__main__":
    unittest.main()
