#!/usr/bin/env python3
"""
ops_shifts.py - SRE On-Call Shift Rotation & Teams Daily Notifier

Rotates on-call shifts on a weekly basis, checks Confluence Team Calendars
for member vacations to prevent overlaps fairly, and notifies a Microsoft
Teams channel daily with an Adaptive Card.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import yaml
from dateutil import tz
from icalendar import Calendar
import recurring_ical_events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ops-shifts")


class OpsShiftsManager:
    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        dry_run: bool = False,
        force_rotation: bool = False,
        mock_calendar: bool = False,
        simulated_now: Optional[dt.datetime] = None,
    ):
        self.config_path = config_path
        self.state_path = state_path
        self.dry_run = dry_run
        self.force_rotation = force_rotation
        self.mock_calendar = mock_calendar
        self.config = self._load_config()
        
        # Setup timezone
        tz_name = self.config.get("rotation", {}).get("timezone", "Europe/Berlin")
        self.local_tz = tz.gettz(tz_name) or dt.timezone.utc
        
        # Setup current time
        if simulated_now:
            if simulated_now.tzinfo is None:
                self.now = simulated_now.replace(tzinfo=self.local_tz)
            else:
                self.now = simulated_now.astimezone(self.local_tz)
        else:
            self.now = dt.datetime.now(self.local_tz)

        self.state = self._load_state()

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found at {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not parse state file {self.state_path}: {e}. Initializing empty state.")

        # Default initial state
        return {
            "last_updated": None,
            "current_shift": None,
            "member_stats": {},
            "history": [],
        }

    def _save_state(self) -> None:
        if self.dry_run:
            logger.info("[Dry Run] Skipping state persistence.")
            return
        self.state["last_updated"] = self.now.isoformat()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        logger.info(f"Updated state saved to {self.state_path}")

    # =========================================================================
    # Calendar Ingestion (Confluence Team Calendars)
    # =========================================================================

    def fetch_calendar_events(self, window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        """
        Fetch vacation events from Confluence Team Calendar.
        First tries .ics export endpoint, falls back to events.json if available.
        """
        if self.mock_calendar:
            logger.info("Using mock calendar data (flag --mock-calendar set).")
            return self._generate_mock_events(window_start, window_end)

        confluence_conf = self.config.get("confluence", {})
        base_url = confluence_conf.get("base_url", "").rstrip("/")
        cal_id = confluence_conf.get("calendar_id", "")
        
        token = os.environ.get("CONFLUENCE_PAT")
        user = os.environ.get("CONFLUENCE_USER")
        password = os.environ.get("CONFLUENCE_PASSWORD")

        if not token and not (user and password):
            logger.warning("No CONFLUENCE_PAT or credentials provided in environment. Falling back to mock calendar.")
            return self._generate_mock_events(window_start, window_end)

        headers = {}
        auth = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif user and password:
            auth = (user, password)

        # 1. Try standard Subcalendar ICS export
        ics_url = f"{base_url}/rest/calendar-services/1.0/calendar/export/subcalendar/{cal_id}.ics"
        try:
            logger.info(f"Fetching calendar feed from {ics_url}...")
            resp = requests.get(ics_url, headers=headers, auth=auth, timeout=15)
            if resp.status_code == 200:
                return self._parse_ics_content(resp.content, window_start, window_end)
            else:
                logger.warning(f"ICS export returned status {resp.status_code}. Attempting events.json fallback.")
        except Exception as e:
            logger.warning(f"Failed to fetch ICS export: {e}. Attempting events.json fallback.")

        # 2. Fallback to events.json
        json_url = f"{base_url}/rest/calendar-services/1.0/calendar/events.json"
        params = {
            "subCalendarId": cal_id,
            "userTimeZoneId": self.config.get("rotation", {}).get("timezone", "Europe/Berlin"),
            "start": f"{window_start.isoformat()}T00:00:00Z",
            "end": f"{window_end.isoformat()}T23:59:59Z",
        }
        try:
            resp = requests.get(json_url, headers=headers, auth=auth, params=params, timeout=15)
            if resp.status_code == 200:
                return self._parse_json_events(resp.json(), window_start, window_end)
            else:
                logger.error(f"events.json returned status {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.error(f"Failed to fetch events.json from Confluence: {e}")

        logger.warning("Could not retrieve calendar events from Confluence. Proceeding with empty vacation list.")
        return []

    def _parse_ics_content(self, ics_bytes: bytes, window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        cal = Calendar.from_ical(ics_bytes)
        dt_start = dt.datetime.combine(window_start, dt.time.min).replace(tzinfo=self.local_tz)
        dt_end = dt.datetime.combine(window_end, dt.time.max).replace(tzinfo=self.local_tz)
        
        events = recurring_ical_events.of(cal).between(dt_start, dt_end)
        parsed = []
        for ev in events:
            summary = str(ev.get("summary", "")).strip()
            start_raw = ev.get("dtstart").dt
            end_raw = ev.get("dtend").dt if ev.get("dtend") else start_raw

            start_date = start_raw if isinstance(start_raw, dt.date) and not isinstance(start_raw, dt.datetime) else start_raw.date()
            end_date = end_raw if isinstance(end_raw, dt.date) and not isinstance(end_raw, dt.datetime) else end_raw.date()

            parsed.append({
                "summary": summary,
                "start": start_date,
                "end": end_date,
            })
        logger.info(f"Parsed {len(parsed)} calendar events from ICS export.")
        return parsed

    def _parse_json_events(self, data: Dict[str, Any], window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        raw_events = data.get("events", [])
        parsed = []
        for item in raw_events:
            summary = item.get("title") or item.get("summary") or ""
            start_str = item.get("start")
            end_str = item.get("end") or start_str
            if not start_str:
                continue
            try:
                start_date = dt.datetime.fromisoformat(start_str.replace("Z", "+00:00")).date()
                end_date = dt.datetime.fromisoformat(end_str.replace("Z", "+00:00")).date()
                parsed.append({
                    "summary": summary,
                    "start": start_date,
                    "end": end_date,
                })
            except Exception as e:
                logger.debug(f"Skipping unparseable event {item}: {e}")
        logger.info(f"Parsed {len(parsed)} calendar events from events.json.")
        return parsed

    def _generate_mock_events(self, window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        """Simulates vacation for the first configured team member in the second week."""
        team = self.config.get("team", [])
        if not team:
            return []
        first_member = team[0]["name"]
        vac_start = window_start + dt.timedelta(days=7)
        vac_end = vac_start + dt.timedelta(days=5)
        return [
            {
                "summary": f"{first_member} Vacation",
                "start": vac_start,
                "end": vac_end,
            }
        ]

    # =========================================================================
    # Fair Scheduling Engine
    # =========================================================================

    def _calculate_shift_window(self, ref_datetime: dt.datetime) -> Tuple[dt.date, dt.date]:
        """
        Calculates the start and end dates for a shift based on the configured weekday.
        Default: Monday (0) to next Monday (0).
        """
        rot_conf = self.config.get("rotation", {})
        start_weekday = rot_conf.get("shift_start_weekday", 0)
        duration_days = rot_conf.get("shift_duration_days", 7)

        ref_date = ref_datetime.date()
        days_ahead = (ref_date.weekday() - start_weekday) % 7
        shift_start = ref_date - dt.timedelta(days=days_ahead)
        shift_end = shift_start + dt.timedelta(days=duration_days)
        return shift_start, shift_end

    def is_member_on_vacation(
        self,
        member: Dict[str, Any],
        events: List[Dict[str, Any]],
        shift_start: dt.date,
        shift_end: dt.date,
        threshold_days: int = 1,
    ) -> Tuple[bool, List[str]]:
        """
        Checks if member has vacation overlapping with the given shift window.
        Returns (is_on_vacation, matching_event_summaries).
        """
        name_patterns = [member["name"].lower()]
        for alias in member.get("aliases", []):
            name_patterns.append(str(alias).lower())

        overlapping_events = []
        total_overlap_days = 0

        for ev in events:
            summary = ev["summary"].lower()
            if any(pattern in summary for pattern in name_patterns):
                ev_start = ev["start"]
                ev_end = ev["end"]
                
                # Check date overlap
                overlap_start = max(ev_start, shift_start)
                overlap_end = min(ev_end, shift_end)
                if overlap_start <= overlap_end:
                    days = (overlap_end - overlap_start).days + 1
                    total_overlap_days += days
                    overlapping_events.append(f"{ev['summary']} ({ev_start} to {ev_end})")

        is_unavailable = total_overlap_days >= threshold_days
        return is_unavailable, overlapping_events

    def pick_fair_candidate(
        self,
        candidates: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        shift_start: dt.date,
        shift_end: dt.date,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Picks the most eligible engineer using Least-Shifts-Completed + Longest-Time-Since-Last-Shift.
        Filters out members on vacation.
        """
        threshold = self.config.get("rotation", {}).get("vacation_overlap_threshold_days", 1)
        member_stats = self.state.setdefault("member_stats", {})

        # Ensure all team members have entries in member_stats
        for m in candidates:
            if m["name"] not in member_stats:
                member_stats[m["name"]] = {"shifts_completed": 0, "last_shift_end": None}

        # 1. Filter out members on vacation
        available = []
        vacation_log = []
        for m in candidates:
            on_vacation, evs = self.is_member_on_vacation(m, events, shift_start, shift_end, threshold)
            if on_vacation:
                vacation_log.append(f"{m['name']} is on vacation: {', '.join(evs)}")
            else:
                available.append(m)

        reason = ""
        if vacation_log:
            logger.info("Vacation exclusions: " + "; ".join(vacation_log))

        if not available:
            logger.warning("All active candidates have conflicting vacations! Falling back to full candidate pool.")
            available = candidates
            reason = "Warning: Selected despite overlapping vacation because all team members are on leave."

        # 2. Sort by:
        #    a) shifts_completed (ascending)
        #    b) last_shift_end (oldest or None first)
        def sort_key(member: Dict[str, Any]):
            stats = member_stats.get(member["name"], {})
            shifts = stats.get("shifts_completed", 0)
            last_end = stats.get("last_shift_end") or "1970-01-01"
            return (shifts, last_end)

        available.sort(key=sort_key)
        selected = available[0]
        logger.info(
            f"Selected fair candidate: {selected['name']} "
            f"(shifts={member_stats[selected['name']]['shifts_completed']}, "
            f"last_end={member_stats[selected['name']]['last_shift_end']})"
        )
        return selected, reason

    def resolve_shifts(self) -> Tuple[Dict[str, Any], Dict[str, Any], bool, List[Dict[str, Any]]]:
        """
        Determines current active shift and previews next week's shift.
        Returns: (current_shift, next_preview, is_handoff_day, calendar_events)
        """
        team = [m for m in self.config.get("team", []) if m.get("active", True)]
        if not team:
            raise ValueError("No active team members configured in 'team' list!")

        shift_start, shift_end = self._calculate_shift_window(self.now)
        next_shift_start = shift_end
        next_shift_end = next_shift_start + dt.timedelta(days=self.config.get("rotation", {}).get("shift_duration_days", 7))

        # Fetch events for the 2-week observation window
        calendar_events = self.fetch_calendar_events(shift_start, next_shift_end)

        current_shift = self.state.get("current_shift")
        is_handoff_day = False

        # Determine if shift rotation is required
        needs_rotation = False
        if not current_shift:
            needs_rotation = True
            is_handoff_day = True
        elif self.force_rotation:
            logger.info("Forced rotation flag active.")
            needs_rotation = True
            is_handoff_day = True
        else:
            curr_end_date = dt.date.fromisoformat(current_shift["end_date"])
            # If current shift window ended or today is the shift start day at/after shift_start_hour
            shift_start_hour = self.config.get("rotation", {}).get("shift_start_hour", 9)
            if self.now.date() >= curr_end_date:
                needs_rotation = True
                is_handoff_day = True
            elif self.now.date() == shift_start and self.now.hour >= shift_start_hour:
                if current_shift.get("start_date") != shift_start.isoformat():
                    needs_rotation = True
                    is_handoff_day = True

        if needs_rotation:
            logger.info(f"Assigning new shift for window {shift_start} to {shift_end}...")
            candidate, note = self.pick_fair_candidate(team, calendar_events, shift_start, shift_end)
            
            # Update member stats
            name = candidate["name"]
            stats = self.state["member_stats"].setdefault(name, {"shifts_completed": 0, "last_shift_end": None})
            stats["shifts_completed"] += 1
            stats["last_shift_end"] = shift_end.isoformat()

            current_shift = {
                "engineer": name,
                "email": candidate.get("teams_email", ""),
                "start_date": shift_start.isoformat(),
                "end_date": shift_end.isoformat(),
                "assigned_at": self.now.isoformat(),
                "note": note,
            }
            self.state["current_shift"] = current_shift
            self.state.setdefault("history", []).append(current_shift)
            self._save_state()
        else:
            logger.info(f"Current shift active: {current_shift['engineer']} (ends {current_shift['end_date']})")

        # Preview next week's candidate (simulating state after current shift)
        preview_candidate, preview_note = self.pick_fair_candidate(
            team, calendar_events, next_shift_start, next_shift_end
        )
        next_preview = {
            "engineer": preview_candidate["name"],
            "email": preview_candidate.get("teams_email", ""),
            "start_date": next_shift_start.isoformat(),
            "end_date": next_shift_end.isoformat(),
            "note": preview_note,
        }

        return current_shift, next_preview, is_handoff_day, calendar_events

    # =========================================================================
    # Microsoft Teams Notification (Adaptive Card)
    # =========================================================================

    def build_teams_adaptive_card(
        self,
        current_shift: Dict[str, Any],
        next_preview: Dict[str, Any],
        is_handoff_day: bool,
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Builds a rich Adaptive Card JSON payload.
        """
        teams_conf = self.config.get("teams", {})
        confluence_conf = self.config.get("confluence", {})
        cal_url = confluence_conf.get("calendar_page_url", "")
        runbook_url = teams_conf.get("runbook_url", "")

        today_str = self.now.strftime("%A, %b %d, %Y")
        title = "🔄 SRE On-Call Shift Handoff" if is_handoff_day else "☀️ SRE Daily On-Call"
        accent_color = "warning" if is_handoff_day else "default"

        curr_start = dt.date.fromisoformat(current_shift["start_date"]).strftime("%b %d")
        curr_end = dt.date.fromisoformat(current_shift["end_date"]).strftime("%b %d, %Y")

        facts = [
            {"title": "Today's Date:", "value": today_str},
            {"title": "Primary On-Call:", "value": f"**{current_shift['engineer']}** ({current_shift.get('email', '')})"},
            {"title": "Active Shift Window:", "value": f"{curr_start} – {curr_end}"},
            {"title": "Next Up (Next Week):", "value": f"{next_preview['engineer']} ({next_preview.get('email', '')})"},
        ]

        # Summarize any team members on vacation during this week
        shift_start = dt.date.fromisoformat(current_shift["start_date"])
        shift_end = dt.date.fromisoformat(current_shift["end_date"])
        vacations_now = []
        for ev in events:
            if max(ev["start"], shift_start) <= min(ev["end"], shift_end):
                vacations_now.append(f"• {ev['summary']} ({ev['start']} to {ev['end']})")

        vacation_text = "\n".join(vacations_now) if vacations_now else "No recorded vacation conflicts this week."

        body: List[Dict[str, Any]] = [
            {
                "type": "Container",
                "style": accent_color,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": title,
                        "weight": "Bolder",
                        "size": "Large",
                        "color": "Accent",
                    },
                    {
                        "type": "TextBlock",
                        "text": "Daily status report and weekly shift rotation briefing for the SRE team.",
                        "isSubtle": True,
                        "spacing": "None",
                    },
                ],
            },
            {
                "type": "FactSet",
                "facts": facts,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "**🌴 Team Vacation Calendar Status (Confluence)**",
                "weight": "Bolder",
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": vacation_text,
                "wrap": True,
                "isSubtle": True,
            },
        ]

        if current_shift.get("note"):
            body.append({
                "type": "TextBlock",
                "text": f"⚠️ *Note: {current_shift['note']}*",
                "color": "Attention",
                "wrap": True,
            })

        actions = []
        if cal_url:
            actions.append({
                "type": "Action.OpenUrl",
                "title": "📅 View SRE Calendar",
                "url": cal_url,
            })
        if runbook_url:
            actions.append({
                "type": "Action.OpenUrl",
                "title": "📖 Open SRE Runbooks",
                "url": runbook_url,
            })

        card_payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "version": "1.4",
                        "type": "AdaptiveCard",
                        "body": body,
                        "actions": actions,
                    },
                }
            ],
        }
        return card_payload

    def notify_teams(self, card_payload: Dict[str, Any]) -> bool:
        webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
        if self.dry_run or not webhook_url:
            logger.info("[Dry Run or Missing TEAMS_WEBHOOK_URL] Teams card payload preview:")
            print(json.dumps(card_payload, indent=2))
            return True

        try:
            logger.info("Posting Adaptive Card to Microsoft Teams...")
            resp = requests.post(webhook_url, json=card_payload, timeout=10)
            if resp.status_code in (200, 202):
                logger.info("Successfully posted on-call notification to Microsoft Teams.")
                return True
            else:
                logger.error(f"Failed to post to Teams. Status {resp.status_code}: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Error posting notification to Microsoft Teams: {e}")
            return False

    def run(self) -> int:
        logger.info(f"Starting Ops Shifts Manager at {self.now.isoformat()}...")
        current_shift, next_preview, is_handoff_day, events = self.resolve_shifts()
        card = self.build_teams_adaptive_card(current_shift, next_preview, is_handoff_day, events)
        success = self.notify_teams(card)
        return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description="Rotate SRE on-call shifts and notify Teams.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(__file__).parent / "state.json",
        help="Path to state.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate run without writing state or posting to Teams webhook.",
    )
    parser.add_argument(
        "--force-rotation",
        action="store_true",
        help="Force rotating to the next shift even if the current shift is active.",
    )
    parser.add_argument(
        "--mock-calendar",
        action="store_true",
        help="Use mock vacation calendar data instead of fetching Confluence.",
    )
    parser.add_argument(
        "--current-time",
        type=str,
        default=None,
        help="Simulate running at a specific ISO datetime (e.g. 2026-11-09T09:15:00).",
    )

    args = parser.parse_args()

    simulated_now = None
    if args.current_time:
        simulated_now = dt.datetime.fromisoformat(args.current_time)

    manager = OpsShiftsManager(
        config_path=args.config,
        state_path=args.state,
        dry_run=args.dry_run,
        force_rotation=args.force_rotation,
        mock_calendar=args.mock_calendar,
        simulated_now=simulated_now,
    )
    sys.exit(manager.run())


if __name__ == "__main__":
    main()
