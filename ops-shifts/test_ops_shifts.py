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
  shift_start_hour: 6
  vacation_overlap_threshold_days: 1

storage:
  backend: "file"

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
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
        )

        member_alice = {"name": "Alice Developer", "aliases": ["Alice", "adeveloper"]}
        member_bob = {"name": "Bob Sysadmin", "aliases": ["Bob", "bsysadmin"]}
        member_charlie = {"name": "Charlie SRE", "aliases": ["Charlie", "csre"]}

        events = [
            {
                "summary": "Alice - Leave (planned)",
                "description": "Family trip",
                "categories": ["Leave (planned)"],
                "attendees": ["Alice Developer"],
                "start": dt.date(2026, 11, 2),
                "end": dt.date(2026, 11, 6),
            },
            {
                "summary": "Travel: Bob Sysadmin",
                "description": "Conference",
                "categories": ["Travel"],
                "attendees": ["Bob Sysadmin"],
                "start": dt.date(2026, 11, 3),
                "end": dt.date(2026, 11, 5),
            },
            {
                "summary": "Charlie Birthday",
                "description": "Happy Birthday Charlie",
                "categories": ["Birthdays"],
                "attendees": ["Charlie SRE"],
                "start": dt.date(2026, 11, 4),
                "end": dt.date(2026, 11, 4),
            },
        ]

        on_vac, evs = manager.is_member_on_vacation(
            member_alice, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        self.assertTrue(on_vac)

        on_vac_bob, _ = manager.is_member_on_vacation(
            member_bob, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        self.assertTrue(on_vac_bob)

        on_vac_charlie, _ = manager.is_member_on_vacation(
            member_charlie, events, dt.date(2026, 11, 2), dt.date(2026, 11, 9)
        )
        self.assertFalse(on_vac_charlie)

    def test_primary_and_backup_assignment(self):
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

        events = [
            {
                "summary": "Alice - Leave",
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
            simulated_now=dt.datetime(2026, 11, 2, 6, 15),
        )
        manager.fetch_calendar_events = lambda s, e: events

        current, next_prev, is_handoff, _ = manager.resolve_shifts()

        self.assertNotEqual(current["engineer"], "Alice Developer")
        self.assertNotEqual(current["backup"], "Alice Developer")
        self.assertNotEqual(current["engineer"], current["backup"])
        self.assertIn(current["engineer"], ["Bob Sysadmin", "Charlie SRE"])
        self.assertIn(current["backup"], ["Bob Sysadmin", "Charlie SRE"])

    def test_confluence_unreachable_fallback_random(self):
        """When Confluence is unreachable, two people are assigned at random with a warning."""
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=False,
            simulated_now=dt.datetime(2026, 11, 2, 6, 15),
        )
        # Simulate calendar unreachable
        manager.calendar_unreachable = True
        manager.fetch_calendar_events = lambda s, e: []

        current, next_prev, is_handoff, _ = manager.resolve_shifts()
        self.assertTrue(is_handoff)
        self.assertIsNotNone(current["engineer"])
        self.assertIsNotNone(current["backup"])
        self.assertNotEqual(current["engineer"], current["backup"])
        self.assertIn("Zufallsauswahl", current["note"])

        # Check that card renders the warning container
        card = manager.build_teams_adaptive_card(current, next_prev, is_handoff, [])
        card_json = json.dumps(card, ensure_ascii=False)
        self.assertIn("Achtung: Zufallsauswahl aktiv!", card_json)

    def test_all_on_leave_fallback_random(self):
        """When all team members are on leave, fallback random assignment triggers."""
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 2, 6, 15),
        )
        # All members on leave
        events = [
            {"summary": "Alice Leave", "categories": ["Leave"], "attendees": ["Alice Developer"], "start": dt.date(2026, 11, 2), "end": dt.date(2026, 11, 9)},
            {"summary": "Bob Leave", "categories": ["Leave"], "attendees": ["Bob Sysadmin"], "start": dt.date(2026, 11, 2), "end": dt.date(2026, 11, 9)},
            {"summary": "Charlie Leave", "categories": ["Leave"], "attendees": ["Charlie SRE"], "start": dt.date(2026, 11, 2), "end": dt.date(2026, 11, 9)},
        ]
        manager.fetch_calendar_events = lambda s, e: events

        current, next_prev, _, _ = manager.resolve_shifts()
        self.assertNotEqual(current["engineer"], current["backup"])
        self.assertIn("Zufallsauswahl", current["note"])

    def test_friday_forecast_vs_monday_schichtwechsel_cards(self):
        """Verify distinct card formatting on Monday (Schichtwechsel) and Friday (Forecast)."""
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

        # 1. Friday test (Forecast / Vorschau)
        manager_fri = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 6, 6, 0),  # Friday
        )
        card_fri = manager_fri.build_teams_adaptive_card(current_shift, next_preview, False, [])
        json_fri = json.dumps(card_fri, ensure_ascii=False)
        self.assertIn("Vorschau", json_fri)
        self.assertIn("Aktuell zuständig (Primär):", json_fri)
        self.assertIn("Nächste Woche ab Montag (Primär):", json_fri)
        self.assertIn("Hinweis zur Folgewoche:", json_fri)

        # 2. Monday test (Schichtwechsel)
        manager_mon = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
            simulated_now=dt.datetime(2026, 11, 9, 6, 0),  # Monday
        )
        card_mon = manager_mon.build_teams_adaptive_card(current_shift, next_preview, True, [])
        json_mon = json.dumps(card_mon, ensure_ascii=False)
        self.assertIn("SRE Betriebsaufgaben: Schichtwechsel", json_mon)
        self.assertIn("Betriebsaufgaben (Primär):", json_mon)
        self.assertIn("Folgewoche (Vorschau):", json_mon)

    def test_weekend_leave_excluded_and_friday_calculation(self):
        """Weekend days (Sat/Sun) must be excluded from workday absence calculations."""
        manager = OpsShiftsManager(
            config_path=self.config_path,
            state_path=self.state_path,
            dry_run=True,
        )
        member = {"name": "Alice Developer", "aliases": ["Alice"]}

        # 1. Weekend-only trip: Saturday Nov 7 to Sunday Nov 8
        weekend_event = [{
            "summary": "Alice Weekend Trip",
            "categories": ["Leave"],
            "attendees": ["Alice Developer"],
            "start": dt.date(2026, 11, 7),  # Saturday
            "end": dt.date(2026, 11, 8),    # Sunday
        }]
        on_leave, evs = manager.is_member_on_vacation(
            member, weekend_event, dt.date(2026, 11, 2), dt.date(2026, 11, 9), threshold_days=1
        )
        self.assertFalse(on_leave, "Weekend absences must NOT mark a member unavailable for weekday shifts!")
        self.assertEqual(len(evs), 0)

        # 2. Leave starting on Friday Nov 6 through Sunday Nov 8 (1 working day overlap: Friday)
        friday_weekend_event = [{
            "summary": "Alice Leave",
            "categories": ["Leave"],
            "attendees": ["Alice Developer"],
            "start": dt.date(2026, 11, 6),  # Friday
            "end": dt.date(2026, 11, 8),    # Sunday
        }]
        # With threshold=1, 1 workday overlap marks unavailable
        on_leave_t1, evs_t1 = manager.is_member_on_vacation(
            member, friday_weekend_event, dt.date(2026, 11, 2), dt.date(2026, 11, 9), threshold_days=1
        )
        self.assertTrue(on_leave_t1)
        self.assertIn("1 Arbeitstag(e)", evs_t1[0])

        # With threshold=2, 1 workday overlap (Friday only) is permitted
        on_leave_t2, evs_t2 = manager.is_member_on_vacation(
            member, friday_weekend_event, dt.date(2026, 11, 2), dt.date(2026, 11, 9), threshold_days=2
        )
        self.assertFalse(on_leave_t2, "With threshold=2, missing only 1 workday (Friday) should remain eligible!")


if __name__ == "__main__":
    unittest.main()
