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
  betriebsaufgaben_url: "https://docs.dbildungscloud.de/betriebsaufgaben"

rotation:
  timezone: "Europe/Berlin"
  shift_duration_days: 7
  shift_start_weekday: 0
  shift_start_hour: 9
  vacation_overlap_threshold_days: 1

vacation:
  event_keywords:
    - "leave"
    - "leave (planned)"
    - "travel"
    - "urlaub"
    - "vacation"
    - "abwesend"
    - "pto"
    - "holiday"
  ignore_keywords:
    - "birthday"
    - "geburtstag"
    - "meeting"
    - "retro"
    - "intern"

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

    def test_leave_and_travel_detection(self):
        """Verify Leave, Leave (planned), and Travel are detected, and Birthdays are ignored."""
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
        )

        member_alice = {"name": "Alice Developer", "aliases": ["Alice", "adeveloper"]}
        member_bob = {"name": "Bob Sysadmin", "aliases": ["Bob", "bsysadmin"]}
        member_charlie = {"name": "Charlie SRE", "aliases": ["Charlie", "csre"]}

        events = [
            # Alice: "Leave (planned)"
            {
                "summary": "Alice - Leave (planned)",
                "description": "Family trip",
                "categories": ["Leave (planned)"],
                "attendees": ["Alice Developer"],
                "start": dt.date(2026, 11, 2),
                "end": dt.date(2026, 11, 6),
            },
            # Bob: "Travel"
            {
                "summary": "Travel: Bob Sysadmin",
                "description": "Conference",
                "categories": ["Travel"],
                "attendees": ["Bob Sysadmin"],
                "start": dt.date(2026, 11, 3),
                "end": dt.date(2026, 11, 5),
            },
            # Charlie: "Birthday" (should be ignored!)
            {
                "summary": "Charlie Birthday",
                "description": "Happy Birthday Charlie",
                "categories": ["Birthdays"],
                "attendees": ["Charlie SRE"],
                "start": dt.date(2026, 11, 4),
                "end": dt.date(2026, 11, 4),
            },
        ]

        # Alice: on leave
        on_vac, evs = manager.is_member_on_vacation(
            member_alice, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        self.assertTrue(on_vac)
        self.assertIn("Leave (planned)", evs[0])

        # Bob: on travel
        on_vac_bob, evs_bob = manager.is_member_on_vacation(
            member_bob, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        self.assertTrue(on_vac_bob)
        self.assertIn("Travel", evs_bob[0])

        # Charlie: birthday is not an absence
        on_vac_charlie, evs_charlie = manager.is_member_on_vacation(
            member_charlie, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        self.assertFalse(on_vac_charlie)
        self.assertEqual(len(evs_charlie), 0)

    def test_primary_and_backup_assignment(self):
        """Verify both Primary and Backup are picked, and neither is on leave."""
        initial_state = {
            "last_updated": "2026-11-01T00:00:00+01:00",
            "current_shift": None,
            "member_stats": {
                "Alice Developer": {"shifts_completed": 0, "last_shift_end": None},
                "Bob Sysadmin": {"shifts_completed": 0, "last_shift_end": None},
                "Charlie SRE": {"shifts_completed": 0, "last_shift_end": None},
            },
            "history": [],
        }
        self.state_path.write_text(json.dumps(initial_state), encoding="utf-8")

        # Alice has "Leave" this week
        events = [
            {
                "summary": "Alice - Leave",
                "description": "Vacation",
                "categories": ["Leave"],
                "attendees": ["Alice Developer"],
                "start": dt.date(2026, 11, 2),
                "end": dt.date(2026, 11, 8),
            }
        ]

        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=False,
            simulated_now=dt.datetime(2026, 11, 2, 9, 30),
        )
        # Mock fetch_calendar_events to return our test events
        manager.fetch_calendar_events = lambda s, e: events

        current, next_prev, is_handoff, _ = manager.resolve_shifts()

        # Alice is on leave, so Primary and Backup must be Bob and Charlie (neither is Alice)
        self.assertNotEqual(current["engineer"], "Alice Developer")
        self.assertNotEqual(current["backup"], "Alice Developer")
        self.assertNotEqual(current["engineer"], current["backup"])
        self.assertIn(current["engineer"], ["Bob Sysadmin", "Charlie SRE"])
        self.assertIn(current["backup"], ["Bob Sysadmin", "Charlie SRE"])

        # Check saved state
        with open(self.state_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["current_shift"]["engineer"], current["engineer"])
        self.assertEqual(saved["current_shift"]["backup"], current["backup"])
        # Verify only the primary had their shift count incremented
        self.assertEqual(saved["member_stats"][current["engineer"]]["shifts_completed"], 1)
        self.assertEqual(saved["member_stats"][current["backup"]]["shifts_completed"], 0)

    def test_backup_fallback_when_next_primary_on_leave(self):
        """If next week's primary is on leave THIS week, alternative backup is picked."""
        initial_state = {
            "last_updated": "2026-11-01T00:00:00+01:00",
            "current_shift": None,
            "member_stats": {
                "Alice Developer": {"shifts_completed": 0, "last_shift_end": None},
                "Bob Sysadmin": {"shifts_completed": 1, "last_shift_end": "2026-10-01"},
                "Charlie SRE": {"shifts_completed": 2, "last_shift_end": "2026-10-08"},
            },
            "history": [],
        }
        self.state_path.write_text(json.dumps(initial_state), encoding="utf-8")

        # Bob (next in line for primary next week) is on "Travel" THIS week (Nov 2-9), but available next week (Nov 9-16)
        events = [
            {
                "summary": "Bob Sysadmin - Travel",
                "categories": ["Travel"],
                "attendees": ["Bob Sysadmin"],
                "start": dt.date(2026, 11, 2),
                "end": dt.date(2026, 11, 6),
            }
        ]

        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 2, 9, 30),
        )
        manager.fetch_calendar_events = lambda s, e: events

        current, next_prev, _, _ = manager.resolve_shifts()

        # Alice has lowest shifts (0) -> Primary
        self.assertEqual(current["engineer"], "Alice Developer")
        # Bob would be next week's primary, but Bob is on travel THIS week!
        # Therefore, Charlie must be picked as current backup:
        self.assertEqual(current["backup"], "Charlie SRE")
        # And next week's primary is still Bob (since his travel ends Nov 6)
        self.assertEqual(next_prev["engineer"], "Bob Sysadmin")

    def test_dynamic_backup_upgrade_for_existing_state(self):
        """If current_shift in state.json lacks 'backup', it is resolved on the fly."""
        initial_state = {
            "last_updated": "2026-11-02T09:00:00+01:00",
            "current_shift": {
                "engineer": "Alice Developer",
                "email": "alice@dbildungscloud.de",
                "start_date": "2026-11-02",
                "end_date": "2026-11-09",
                "assigned_at": "2026-11-02T09:00:00+01:00",
            },
            "member_stats": {
                "Alice Developer": {"shifts_completed": 1, "last_shift_end": "2026-11-09"},
                "Bob Sysadmin": {"shifts_completed": 0, "last_shift_end": None},
                "Charlie SRE": {"shifts_completed": 0, "last_shift_end": None},
            },
            "history": [],
        }
        self.state_path.write_text(json.dumps(initial_state), encoding="utf-8")

        # Running mid-week on Wednesday (no rotation due)
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=False,
            simulated_now=dt.datetime(2026, 11, 4, 10, 0),
        )
        manager.fetch_calendar_events = lambda s, e: []

        current, _, is_handoff, _ = manager.resolve_shifts()
        self.assertFalse(is_handoff)
        self.assertEqual(current["engineer"], "Alice Developer")
        self.assertIsNotNone(current.get("backup"))

        # Verify saved state now includes backup
        with open(self.state_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("backup", saved["current_shift"])

    def test_german_adaptive_card_structure(self):
        """Verify the Teams card is correctly localized in German with Betriebsaufgaben link."""
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 4, 8, 30),
        )

        current_shift = {
            "engineer": "Alice Developer",
            "email": "alice@dbildungscloud.de",
            "backup": "Bob Sysadmin",
            "backup_email": "bob@dbildungscloud.de",
            "start_date": "2026-11-02",
            "end_date": "2026-11-09",
        }
        next_preview = {
            "engineer": "Bob Sysadmin",
            "email": "bob@dbildungscloud.de",
            "backup": "Charlie SRE",
            "backup_email": "charlie@dbildungscloud.de",
            "start_date": "2026-11-09",
            "end_date": "2026-11-16",
        }
        events = [
            {
                "summary": "Charlie SRE - Urlaub",
                "start": dt.date(2026, 11, 4),
                "end": dt.date(2026, 11, 6),
            }
        ]

        card = manager.build_teams_adaptive_card(
            current_shift=current_shift,
            next_preview=next_preview,
            is_handoff_day=False,
            events=events,
        )

        card_json = json.dumps(card, ensure_ascii=False)

        # Check German titles and labels
        self.assertIn("☀️ OPS-Tätigkeiten Tagesübersicht", card_json)
        self.assertIn("Bereitschaft (Primär):", card_json)
        self.assertIn("Bereitschaft (Backup):", card_json)
        self.assertIn("Aktuelle Schicht:", card_json)
        self.assertIn("Nächste Woche (Vorschau):", card_json)
        self.assertIn("Mittwoch, 04. November 2026", card_json)
        self.assertIn("02. Nov. – 09. Nov. 2026", card_json)
        self.assertIn("Abwesenheiten & Urlaub im Team", card_json)

        # Check button actions and Betriebsaufgaben link
        self.assertIn("📅 SRE-Kalender öffnen", card_json)
        self.assertIn("📋 Betriebsaufgaben öffnen", card_json)


if __name__ == "__main__":
    unittest.main()
