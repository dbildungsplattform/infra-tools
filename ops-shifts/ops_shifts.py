#!/usr/bin/env python3
"""
ops_shifts.py - SRE On-Call Shift Rotation & Teams Daily Notifier

Rotates on-call shifts on a weekly basis, checks Confluence Team Calendars
for member leaves/vacations/travel to prevent overlaps fairly, assigns both
a Primary and Backup on-call engineer, and notifies a Microsoft Teams channel
daily with an Adaptive Card.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

# Timezone support: standard library zoneinfo (Python 3.9+)
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from dateutil.tz import gettz as ZoneInfo
    except ImportError:
        ZoneInfo = None

# HTTP support: requests with urllib fallback
try:
    import requests
except ImportError:
    requests = None
    import urllib.error
    import urllib.parse
    import urllib.request

# iCalendar support: icalendar with basic regex fallback
try:
    from icalendar import Calendar
    import recurring_ical_events
    HAS_ICALENDAR = True
except ImportError:
    HAS_ICALENDAR = False

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
        if ZoneInfo is not None:
            self.local_tz = ZoneInfo(tz_name)
        else:
            self.local_tz = dt.timezone.utc

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
        Fetch vacation and leave events from Confluence Team Calendar.
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
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # 1. Try standard Subcalendar ICS export
        ics_url = f"{base_url}/rest/calendar-services/1.0/calendar/export/subcalendar/{cal_id}.ics"
        try:
            logger.info(f"Fetching calendar feed from {ics_url}...")
            content = self._http_get(ics_url, headers=headers, user=user, password=password)
            if content:
                return self._parse_ics_content(content, window_start, window_end)
        except Exception as e:
            logger.warning(f"Failed to fetch ICS export: {e}. Attempting events.json fallback.")

        # 2. Fallback to events.json
        tz_name = self.config.get("rotation", {}).get("timezone", "Europe/Berlin")
        json_url = (
            f"{base_url}/rest/calendar-services/1.0/calendar/events.json?"
            f"subCalendarId={cal_id}&userTimeZoneId={tz_name}&"
            f"start={window_start.isoformat()}T00:00:00Z&end={window_end.isoformat()}T23:59:59Z"
        )
        try:
            resp_bytes = self._http_get(json_url, headers=headers, user=user, password=password)
            if resp_bytes:
                data = json.loads(resp_bytes.decode("utf-8"))
                return self._parse_json_events(data, window_start, window_end)
        except Exception as e:
            logger.error(f"Failed to fetch events.json from Confluence: {e}")

        logger.warning("Could not retrieve calendar events from Confluence. Proceeding with empty vacation list.")
        return []

    def _http_get(self, url: str, headers: Dict[str, str], user: Optional[str], password: Optional[str]) -> Optional[bytes]:
        if requests is not None:
            auth = (user, password) if user and password else None
            resp = requests.get(url, headers=headers, auth=auth, timeout=15)
            if resp.status_code == 200:
                return resp.content
            logger.warning(f"HTTP GET {url} returned status {resp.status_code}")
            return None
        else:
            req = urllib.request.Request(url, headers=headers)
            if user and password:
                import base64
                creds = base64.b64encode(f"{user}:{password}".encode()).decode()
                req.add_header("Authorization", f"Basic {creds}")
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()

    def _parse_ics_content(self, ics_bytes: bytes, window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        if HAS_ICALENDAR:
            cal = Calendar.from_ical(ics_bytes)
            dt_start = dt.datetime.combine(window_start, dt.time.min).replace(tzinfo=self.local_tz)
            dt_end = dt.datetime.combine(window_end, dt.time.max).replace(tzinfo=self.local_tz)

            events = recurring_ical_events.of(cal).between(dt_start, dt_end)
            parsed = []
            for ev in events:
                summary = str(ev.get("summary", "")).strip()
                description = str(ev.get("description", "")).strip()

                categories_raw = ev.get("categories")
                categories = []
                if categories_raw:
                    if isinstance(categories_raw, list):
                        categories = [str(c).strip() for c in categories_raw]
                    else:
                        categories = [str(categories_raw).strip()]

                attendees = []
                if ev.get("attendee"):
                    att_raw = ev.get("attendee")
                    if not isinstance(att_raw, list):
                        att_raw = [att_raw]
                    for a in att_raw:
                        attendees.append(str(a))
                        if hasattr(a, "params") and "CN" in a.params:
                            attendees.append(str(a.params["CN"]))

                start_raw = ev.get("dtstart").dt
                end_raw = ev.get("dtend").dt if ev.get("dtend") else start_raw

                start_date = start_raw if isinstance(start_raw, dt.date) and not isinstance(start_raw, dt.datetime) else start_raw.date()
                end_date = end_raw if isinstance(end_raw, dt.date) and not isinstance(end_raw, dt.datetime) else end_raw.date()

                parsed.append({
                    "summary": summary,
                    "description": description,
                    "categories": categories,
                    "attendees": attendees,
                    "start": start_date,
                    "end": end_date,
                })
            logger.info(f"Parsed {len(parsed)} calendar events via icalendar.")
            return parsed
        else:
            return self._parse_ics_fallback(ics_bytes.decode("utf-8", errors="ignore"), window_start, window_end)

    def _parse_ics_fallback(self, ics_text: str, window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        """Lightweight regex-based fallback for environments without icalendar package."""
        unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
        events = []
        current: Optional[Dict[str, Any]] = None

        for line in unfolded.splitlines():
            line = line.strip()
            if line == "BEGIN:VEVENT":
                current = {"summary": "", "description": "", "categories": [], "attendees": []}
            elif line == "END:VEVENT" and current:
                if "start" in current:
                    if "end" not in current:
                        current["end"] = current["start"]
                    # Check window intersection
                    if current["start"] <= window_end and current["end"] >= window_start:
                        events.append(current)
                current = None
            elif current is not None and ":" in line:
                key_part, val = line.split(":", 1)
                prop = key_part.split(";")[0].upper()
                if prop == "SUMMARY":
                    current["summary"] = val
                elif prop == "DESCRIPTION":
                    current["description"] = val
                elif prop == "CATEGORIES":
                    current["categories"].extend([c.strip() for c in val.split(",")])
                elif prop in ("DTSTART", "DTEND"):
                    try:
                        val_clean = val.replace("Z", "").split("T")[0]
                        d = dt.date(int(val_clean[:4]), int(val_clean[4:6]), int(val_clean[6:8]))
                        if prop == "DTSTART":
                            current["start"] = d
                        else:
                            current["end"] = d
                    except Exception:
                        pass
                elif prop in ("ATTENDEE", "ORGANIZER"):
                    current["attendees"].append(val)

        logger.info(f"Parsed {len(events)} calendar events via fallback parser.")
        return events

    def _parse_json_events(self, data: Dict[str, Any], window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        raw_events = data.get("events", [])
        parsed = []
        for item in raw_events:
            summary = item.get("title") or item.get("summary") or ""
            description = item.get("description") or ""
            event_type = item.get("eventType") or item.get("subCalendarType") or ""
            who = item.get("who") or item.get("author") or item.get("invitees") or []
            if isinstance(who, list):
                who_list = [str(w.get("name") if isinstance(w, dict) else w) for w in who]
            else:
                who_list = [str(who)]

            start_str = item.get("start")
            end_str = item.get("end") or start_str
            if not start_str:
                continue
            try:
                start_date = dt.datetime.fromisoformat(start_str.replace("Z", "+00:00")).date()
                end_date = dt.datetime.fromisoformat(end_str.replace("Z", "+00:00")).date()
                parsed.append({
                    "summary": summary,
                    "description": description,
                    "categories": [event_type] if event_type else [],
                    "attendees": who_list,
                    "start": start_date,
                    "end": end_date,
                })
            except Exception as e:
                logger.debug(f"Skipping unparseable event {item}: {e}")
        logger.info(f"Parsed {len(parsed)} calendar events from events.json.")
        return parsed

    def _generate_mock_events(self, window_start: dt.date, window_end: dt.date) -> List[Dict[str, Any]]:
        """Simulates vacation, leave (planned), and travel events."""
        team = self.config.get("team", [])
        if not team:
            return []

        events = []
        # First member: "Leave (planned)" during week 2
        first_member = team[0]["name"]
        vac_start = window_start + dt.timedelta(days=7)
        vac_end = vac_start + dt.timedelta(days=5)
        events.append({
            "summary": f"{first_member} - Leave (planned)",
            "description": "Annual vacation",
            "categories": ["Leave (planned)"],
            "attendees": [first_member],
            "start": vac_start,
            "end": vac_end,
        })

        # Second member (if exists): "Travel" event in week 1
        if len(team) > 1:
            second_member = team[1]["name"]
            travel_start = window_start + dt.timedelta(days=1)
            travel_end = travel_start + dt.timedelta(days=2)
            events.append({
                "summary": f"Conference Travel: {second_member}",
                "description": "KubeCon attendance",
                "categories": ["Travel"],
                "attendees": [second_member],
                "start": travel_start,
                "end": travel_end,
            })

        return events

    # =========================================================================
    # Fair Scheduling & Backup Engine
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
        Checks if member has vacation, leave, leave (planned), or travel overlapping
        with the given shift window.
        Returns (is_on_vacation, matching_event_summaries).
        """
        vac_conf = self.config.get("vacation", {})
        default_keywords = ["leave", "leave (planned)", "travel", "urlaub", "vacation", "abwesend", "pto", "holiday"]
        event_keywords = [k.lower() for k in vac_conf.get("event_keywords", default_keywords)]
        default_ignores = ["birthday", "geburtstag", "meeting", "retro", "intern"]
        ignore_keywords = [k.lower() for k in vac_conf.get("ignore_keywords", default_ignores)]

        name_patterns = [member["name"].lower()]
        for alias in member.get("aliases", []):
            name_patterns.append(str(alias).lower())

        overlapping_events = []
        total_overlap_days = 0

        for ev in events:
            summary = ev.get("summary", "").lower()
            description = ev.get("description", "").lower()
            categories = [c.lower() for c in ev.get("categories", [])]
            attendees = [a.lower() for a in ev.get("attendees", [])]

            combined_text = f"{summary} {description} {' '.join(categories)} {' '.join(attendees)}"

            # 1. Skip ignored event types (e.g. Birthdays)
            if any(ign in summary or any(ign in cat for cat in categories) for ign in ignore_keywords):
                continue

            # 2. Check if this member is identified in the event
            member_matched = (
                any(p in summary for p in name_patterns)
                or any(p in description for p in name_patterns)
                or any(any(p in a for p in name_patterns) for a in attendees)
            )

            # 3. Check if event represents Leave / Travel / Vacation
            is_leave_event = (
                any(kw in summary for kw in event_keywords)
                or any(kw in description for kw in event_keywords)
                or any(any(kw in cat for kw in event_keywords) for cat in categories)
                or not any(ign in combined_text for ign in ignore_keywords)
            )

            if member_matched and is_leave_event:
                ev_start = ev["start"]
                ev_end = ev["end"]

                # Check date overlap with shift window
                overlap_start = max(ev_start, shift_start)
                overlap_end = min(ev_end, shift_end)
                if overlap_start <= overlap_end:
                    days = (overlap_end - overlap_start).days + 1
                    total_overlap_days += days
                    overlapping_events.append(f"{ev.get('summary', 'Leave')} ({ev_start} to {ev_end})")

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
        Picks the primary on-call engineer using Least-Shifts-Completed + Longest-Time-Since-Last-Shift.
        Filters out members on leave/travel.
        """
        threshold = self.config.get("rotation", {}).get("vacation_overlap_threshold_days", 1)
        member_stats = self.state.setdefault("member_stats", {})

        # Ensure all team members have entries in member_stats
        for m in candidates:
            if m["name"] not in member_stats:
                member_stats[m["name"]] = {"shifts_completed": 0, "last_shift_end": None}

        # Filter out members on leave/travel
        available = []
        vacation_log = []
        for m in candidates:
            on_vacation, evs = self.is_member_on_vacation(m, events, shift_start, shift_end, threshold)
            if on_vacation:
                vacation_log.append(f"{m['name']} is on leave/travel: {', '.join(evs)}")
            else:
                available.append(m)

        reason = ""
        if vacation_log:
            logger.info("Leave/travel exclusions: " + "; ".join(vacation_log))

        if not available:
            logger.warning("All active candidates have conflicting leaves! Falling back to full candidate pool.")
            available = candidates
            reason = "Warning: Selected despite leave because all team members have overlapping leave."

        # Sort by: 1) shifts_completed (ascending), 2) last_shift_end (oldest or None first)
        def sort_key(member: Dict[str, Any]):
            stats = member_stats.get(member["name"], {})
            shifts = stats.get("shifts_completed", 0)
            last_end = stats.get("last_shift_end") or "1970-01-01"
            return (shifts, last_end)

        available.sort(key=sort_key)
        selected = available[0]
        logger.info(
            f"Selected fair primary candidate: {selected['name']} "
            f"(shifts={member_stats[selected['name']]['shifts_completed']}, "
            f"last_end={member_stats[selected['name']]['last_shift_end']})"
        )
        return selected, reason

    def pick_backup_candidate(
        self,
        candidates: List[Dict[str, Any]],
        primary_candidate: Dict[str, Any],
        next_primary_candidate: Optional[Dict[str, Any]],
        events: List[Dict[str, Any]],
        shift_start: dt.date,
        shift_end: dt.date,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Selects the backup on-call engineer for the current shift.
        Best practice: Uses the engineer scheduled to be primary for the FOLLOWING week,
        provided they are not on leave/travel during the CURRENT shift week.
        If unavailable, chooses the next eligible candidate from the queue.
        """
        threshold = self.config.get("rotation", {}).get("vacation_overlap_threshold_days", 1)
        pool = [m for m in candidates if m["name"] != primary_candidate["name"]]
        if not pool:
            return primary_candidate, "Only one team member available (serves as primary and backup)."

        # 1. Preferred choice: Next week's primary engineer (if available this week)
        if next_primary_candidate and next_primary_candidate["name"] != primary_candidate["name"]:
            on_leave, evs = self.is_member_on_vacation(next_primary_candidate, events, shift_start, shift_end, threshold)
            if not on_leave:
                logger.info(f"Selected backup candidate: {next_primary_candidate['name']} (next week's primary).")
                return next_primary_candidate, "Next week's primary serves as current backup."
            else:
                logger.info(f"Next week's primary {next_primary_candidate['name']} is on leave this week: {evs}. Picking alternative backup.")

        # 2. Alternative choice: Filter remaining pool for candidates not on leave this week
        available = []
        for m in pool:
            on_leave, _ = self.is_member_on_vacation(m, events, shift_start, shift_end, threshold)
            if not on_leave:
                available.append(m)

        if not available:
            logger.warning("All backup candidates have conflicting leaves this week! Falling back to pool.")
            available = pool
            return available[0], "Warning: Backup selected despite leave (all candidates on leave)."

        member_stats = self.state.setdefault("member_stats", {})
        def sort_key(member: Dict[str, Any]):
            stats = member_stats.get(member["name"], {})
            shifts = stats.get("shifts_completed", 0)
            last_end = stats.get("last_shift_end") or "1970-01-01"
            return (shifts, last_end)

        available.sort(key=sort_key)
        selected = available[0]
        logger.info(f"Selected backup candidate: {selected['name']} (runner-up queue).")
        return selected, "Runner-up selected as backup."

    def resolve_shifts(self) -> Tuple[Dict[str, Any], Dict[str, Any], bool, List[Dict[str, Any]]]:
        """
        Determines current active shift (Primary + Backup) and previews next week's shift.
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
            primary_candidate, primary_note = self.pick_fair_candidate(team, calendar_events, shift_start, shift_end)

            # Update member stats for primary
            name = primary_candidate["name"]
            stats = self.state["member_stats"].setdefault(name, {"shifts_completed": 0, "last_shift_end": None})
            stats["shifts_completed"] += 1
            stats["last_shift_end"] = shift_end.isoformat()

            # Pre-calculate next week's primary to pick the backup
            next_primary, _ = self.pick_fair_candidate(team, calendar_events, next_shift_start, next_shift_end)
            backup_candidate, backup_note = self.pick_backup_candidate(
                team, primary_candidate, next_primary, calendar_events, shift_start, shift_end
            )

            current_shift = {
                "engineer": name,
                "email": primary_candidate.get("teams_email", ""),
                "backup": backup_candidate["name"],
                "backup_email": backup_candidate.get("teams_email", ""),
                "start_date": shift_start.isoformat(),
                "end_date": shift_end.isoformat(),
                "assigned_at": self.now.isoformat(),
                "note": primary_note,
                "backup_note": backup_note,
            }
            self.state["current_shift"] = current_shift
            self.state.setdefault("history", []).append(current_shift)
            self._save_state()
        else:
            # Shift is already active: verify backup is present in state
            if "backup" not in current_shift or not current_shift["backup"]:
                logger.info("Current shift is missing backup engineer. Resolving backup now...")
                primary_name = current_shift["engineer"]
                primary_cand = next((m for m in team if m["name"] == primary_name), {"name": primary_name})
                next_primary, _ = self.pick_fair_candidate(team, calendar_events, next_shift_start, next_shift_end)
                backup_candidate, backup_note = self.pick_backup_candidate(
                    team, primary_cand, next_primary, calendar_events, shift_start, shift_end
                )
                current_shift["backup"] = backup_candidate["name"]
                current_shift["backup_email"] = backup_candidate.get("teams_email", "")
                current_shift["backup_note"] = backup_note
                self.state["current_shift"] = current_shift
                self._save_state()

            logger.info(
                f"Current shift active: Primary={current_shift['engineer']}, "
                f"Backup={current_shift['backup']} (ends {current_shift['end_date']})"
            )

        # Pre-calculate preview for next week (Primary + Backup)
        next_primary, next_primary_note = self.pick_fair_candidate(
            team, calendar_events, next_shift_start, next_shift_end
        )
        week_after_start = next_shift_end
        week_after_end = week_after_start + dt.timedelta(days=self.config.get("rotation", {}).get("shift_duration_days", 7))
        week_after_primary, _ = self.pick_fair_candidate(team, calendar_events, week_after_start, week_after_end)
        next_backup, next_backup_note = self.pick_backup_candidate(
            team, next_primary, week_after_primary, calendar_events, next_shift_start, next_shift_end
        )

        next_preview = {
            "engineer": next_primary["name"],
            "email": next_primary.get("teams_email", ""),
            "backup": next_backup["name"],
            "backup_email": next_backup.get("teams_email", ""),
            "start_date": next_shift_start.isoformat(),
            "end_date": next_shift_end.isoformat(),
            "note": next_primary_note,
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
        Builds a rich Adaptive Card JSON payload displaying both Primary and Backup.
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

        primary_str = f"**{current_shift['engineer']}** ({current_shift.get('email', '')})"
        backup_str = f"**{current_shift.get('backup', 'None')}** ({current_shift.get('backup_email', '')})"
        next_str = (
            f"**{next_preview['engineer']}** (Primary) | "
            f"**{next_preview.get('backup', 'None')}** (Backup)"
        )

        facts = [
            {"title": "Today's Date:", "value": today_str},
            {"title": "Primary On-Call:", "value": primary_str},
            {"title": "Backup On-Call:", "value": backup_str},
            {"title": "Active Shift Window:", "value": f"{curr_start} – {curr_end}"},
            {"title": "Next Up (Next Week):", "value": next_str},
        ]

        # Summarize team members on leave/travel during this week
        shift_start = dt.date.fromisoformat(current_shift["start_date"])
        shift_end = dt.date.fromisoformat(current_shift["end_date"])
        vacations_now = []
        for ev in events:
            if max(ev["start"], shift_start) <= min(ev["end"], shift_end):
                vacations_now.append(f"• {ev.get('summary', 'Leave')} ({ev['start']} to {ev['end']})")

        vacation_text = "\n".join(vacations_now) if vacations_now else "No recorded leave or travel conflicts this week."

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
                "text": "**🌴 Team Leave & Travel Status (Confluence)**",
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

        notes = []
        if current_shift.get("note"):
            notes.append(f"Primary: {current_shift['note']}")
        if current_shift.get("backup_note") and "Warning" in current_shift["backup_note"]:
            notes.append(f"Backup: {current_shift['backup_note']}")

        if notes:
            body.append({
                "type": "TextBlock",
                "text": "⚠️ *" + " | ".join(notes) + "*",
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
            if requests is not None:
                resp = requests.post(webhook_url, json=card_payload, timeout=10)
                if resp.status_code in (200, 202):
                    logger.info("Successfully posted on-call notification to Microsoft Teams.")
                    return True
                else:
                    logger.error(f"Failed to post to Teams. Status {resp.status_code}: {resp.text}")
                    return False
            else:
                data = json.dumps(card_payload).encode("utf-8")
                req = urllib.request.Request(
                    webhook_url, data=data, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status in (200, 202):
                        logger.info("Successfully posted on-call notification to Microsoft Teams.")
                        return True
                    else:
                        logger.error(f"Failed to post to Teams. Status {resp.status}")
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
        help="Use mock vacation calendar data instead of calling Confluence.",
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
