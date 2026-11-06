#!/usr/bin/env python3
"""
ops_shifts.py - SRE Betriebsaufgaben Shift Rotation & Teams Notifier

Rotates weekly operational duty shifts (Betriebsaufgaben, Monday to Friday)
among SRE team members fairly, checks Confluence Team Calendars for leaves/vacation
during working days (excluding weekends), assigns both a Primary and Backup engineer,
persists state to a local JSON file (caller-managed, e.g. via GitHub Variables in CI),
includes random fallback assignment if Confluence is unreachable or candidates are
unavailable, and sends notifications to Microsoft Teams on Mondays (Schichtwechsel)
and Fridays (Forecast) at 06:00 CET.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
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
        card_type: str = "auto",
        simulated_now: Optional[dt.datetime] = None,
    ):
        self.config_path = config_path
        self.state_path = state_path
        self.dry_run = dry_run
        self.force_rotation = force_rotation
        self.card_type = card_type
        self.config = self._load_config()

        self.calendar_unreachable = False
        self.fallback_triggered = False

        # Setup timezone (Default: Europe/Berlin)
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

    # =========================================================================
    # State Persistence (Separation of Concerns: Local File I/O)
    # =========================================================================

    def _load_state(self) -> Dict[str, Any]:
        default_state = {
            "last_updated": None,
            "current_shift": None,
            "member_stats": {},
            "history": [],
        }

        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        for k, v in default_state.items():
                            loaded.setdefault(k, v)
                        logger.info(f"Loaded state from {self.state_path}")
                        return loaded
            except Exception as e:
                logger.warning(f"Could not parse state file {self.state_path}: {e}")

        return default_state

    def _save_state(self) -> None:
        if self.dry_run:
            logger.info("[Dry Run] Skipping state persistence.")
            return

        self.state["last_updated"] = self.now.isoformat()
        # Keep history trimmed to last 52 shifts to maintain lightweight state size
        if "history" in self.state and len(self.state["history"]) > 52:
            self.state["history"] = self.state["history"][-52:]

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
        Flags self.calendar_unreachable if Confluence fails.
        """
        confluence_conf = self.config.get("confluence", {})
        base_url = confluence_conf.get("base_url", "").rstrip("/")
        cal_id = confluence_conf.get("calendar_id", "")

        token = os.environ.get("CONFLUENCE_PAT")
        user = os.environ.get("CONFLUENCE_USER")
        password = os.environ.get("CONFLUENCE_PASSWORD")

        if not token and not (user and password):
            logger.error("CONFLUENCE_PAT (or user/password) is not configured! Cannot fetch Confluence calendar.")
            self.calendar_unreachable = True
            return []

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

        logger.error("Confluence calendar is unreachable! Triggering fallback mode.")
        self.calendar_unreachable = True
        return []

    def _http_get(self, url: str, headers: Dict[str, str], user: Optional[str] = None, password: Optional[str] = None) -> Optional[bytes]:
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

                start_date = start_raw.date() if isinstance(start_raw, dt.datetime) else start_raw

                # RFC 5545: All-day / midnight DTEND is non-inclusive (the day/second after).
                # E.g. DTSTART:20261113 DTEND:20261114 is an all-day event for Nov 13 only.
                if isinstance(end_raw, dt.datetime):
                    if end_raw.time() == dt.time(0, 0) and end_raw.date() > start_date:
                        end_date = end_raw.date() - dt.timedelta(days=1)
                    else:
                        end_date = end_raw.date()
                else:
                    if end_raw > start_date:
                        end_date = end_raw - dt.timedelta(days=1)
                    else:
                        end_date = end_raw

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
                    elif current["end"] > current["start"]:
                        # Non-inclusive end date for all-day events
                        current["end"] = current["end"] - dt.timedelta(days=1)
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
                start_dt = dt.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                end_dt = dt.datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                start_date = start_dt.date()
                if end_dt.time() == dt.time(0, 0) and end_dt.date() > start_date:
                    end_date = end_dt.date() - dt.timedelta(days=1)
                else:
                    end_date = end_dt.date()
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

    # =========================================================================
    # Scheduling, Vacation Filtering & Fallback Engine
    # =========================================================================

    def _calculate_shift_window(self, ref_datetime: dt.datetime) -> Tuple[dt.date, dt.date]:
        rot_conf = self.config.get("rotation", {})
        start_weekday = rot_conf.get("shift_start_weekday", 0)  # 0 = Monday
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
        vac_conf = self.config.get("vacation", {})
        default_keywords = ["leave", "leave (planned)", "travel", "urlaub", "vacation", "abwesend", "pto", "holiday"]
        event_keywords = [k.lower() for k in vac_conf.get("event_keywords", default_keywords)]
        default_ignores = ["birthday", "geburtstag", "meeting", "retro", "intern", "daily", "refinement", "pi ", "quarter cap"]
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

            if any(ign in summary or any(ign in cat for cat in categories) for ign in ignore_keywords):
                continue

            member_matched = (
                any(p in summary for p in name_patterns)
                or any(p in description for p in name_patterns)
                or any(any(p in a for p in name_patterns) for a in attendees)
            )

            is_leave_event = (
                any(kw in summary for kw in event_keywords)
                or any(kw in description for kw in event_keywords)
                or any(any(kw in cat for kw in event_keywords) for cat in categories)
                or not any(ign in combined_text for ign in ignore_keywords)
            )

            if member_matched and is_leave_event:
                ev_start = ev["start"]
                ev_end = ev["end"]

                # Overlap between event and the shift week workdays (Mon-Fri)
                # Shift runs from shift_start (Mon) to shift_end (next Mon); workdays end on Friday.
                overlap_start = max(ev_start, shift_start)
                overlap_end = min(ev_end, shift_end - dt.timedelta(days=1) if shift_end > shift_start else shift_end)

                cur = overlap_start
                overlap_workdays = 0
                while cur <= overlap_end:
                    if cur.weekday() < 5:  # Monday to Friday only (exclude Saturday & Sunday)
                        overlap_workdays += 1
                    cur += dt.timedelta(days=1)

                if overlap_workdays > 0:
                    total_overlap_days += overlap_workdays
                    overlapping_events.append(
                        f"{ev.get('summary', 'Urlaub')} ({ev_start} bis {ev_end}, {overlap_workdays} Arbeitstag(e))"
                    )

        is_unavailable = total_overlap_days >= threshold_days
        return is_unavailable, overlapping_events

    def pick_fallback_candidates(
        self,
        candidates: List[Dict[str, Any]],
        reason: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        """
        Fallback mechanism: assigns 2 people at random from the active team list.
        Triggered when Confluence is unreachable or no available person is found.
        """
        self.fallback_triggered = True
        logger.warning(f"Zufallsauswahl aktiviert! Grund: {reason}")
        if not candidates:
            raise ValueError("Kandidatenliste ist leer! Keine Zufallsauswahl möglich.")

        if len(candidates) >= 2:
            primary, backup = random.sample(candidates, 2)
        else:
            primary = candidates[0]
            backup = candidates[0]

        note = f"⚠️ Zufallsauswahl ({reason})"
        return primary, backup, note

    def pick_fair_candidate(
        self,
        candidates: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        shift_start: dt.date,
        shift_end: dt.date,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Picks the primary on-call engineer using Least-Shifts-Completed + Longest-Time-Since-Last-Shift.
        Returns (candidate, note) or (None, reason) if all are on leave.
        """
        threshold = self.config.get("rotation", {}).get("vacation_overlap_threshold_days", 1)
        member_stats = self.state.setdefault("member_stats", {})

        for m in candidates:
            if m["name"] not in member_stats:
                member_stats[m["name"]] = {"shifts_completed": 0, "last_shift_end": None}

        available = []
        vacation_log = []
        for m in candidates:
            on_vacation, evs = self.is_member_on_vacation(m, events, shift_start, shift_end, threshold)
            if on_vacation:
                vacation_log.append(f"{m['name']}: {', '.join(evs)}")
            else:
                available.append(m)

        if vacation_log:
            logger.info("Abwesenheiten ausgeschlossen: " + "; ".join(vacation_log))

        if not available:
            logger.warning("Keine verfügbaren Personen gefunden (alle im Urlaub/abwesend).")
            return None, "Keine Person verfügbar (alle abwesend)"

        def sort_key(member: Dict[str, Any]):
            stats = member_stats.get(member["name"], {})
            shifts = stats.get("shifts_completed", 0)
            last_end = stats.get("last_shift_end") or "1970-01-01"
            return (shifts, last_end)

        available.sort(key=sort_key)
        selected = available[0]
        logger.info(
            f"Regulär ausgewählter Primär-Kandidat: {selected['name']} "
            f"(shifts={member_stats[selected['name']]['shifts_completed']})"
        )
        return selected, ""

    def pick_backup_candidate(
        self,
        candidates: List[Dict[str, Any]],
        primary_candidate: Dict[str, Any],
        next_primary_candidate: Optional[Dict[str, Any]],
        events: List[Dict[str, Any]],
        shift_start: dt.date,
        shift_end: dt.date,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Selects the backup on-call engineer.
        Uses next week's primary if available this week; otherwise selects runner-up.
        """
        threshold = self.config.get("rotation", {}).get("vacation_overlap_threshold_days", 1)
        pool = [m for m in candidates if m["name"] != primary_candidate["name"]]
        if not pool:
            return primary_candidate, "Nur eine Person im Team."

        # Preferred choice: Next week's primary (if not on vacation this week)
        if next_primary_candidate and next_primary_candidate["name"] != primary_candidate["name"]:
            on_leave, evs = self.is_member_on_vacation(next_primary_candidate, events, shift_start, shift_end, threshold)
            if not on_leave:
                logger.info(f"Backup ausgewählt: {next_primary_candidate['name']} (Primär-Kandidat der Folgewoche).")
                return next_primary_candidate, "Primär-Kandidat der Folgewoche dient als Backup."
            else:
                logger.info(f"Folgewochen-Primär {next_primary_candidate['name']} ist diese Woche abwesend: {evs}.")

        # Runner-up choice: Filter remaining pool for members not on leave this week
        available = []
        for m in pool:
            on_leave, _ = self.is_member_on_vacation(m, events, shift_start, shift_end, threshold)
            if not on_leave:
                available.append(m)

        if not available:
            logger.warning("Kein reguläres Backup gefunden (alle restlichen Kandidaten abwesend).")
            return None, "Kein Backup verfügbar (alle restlichen Kandidaten abwesend)"

        member_stats = self.state.setdefault("member_stats", {})
        def sort_key(member: Dict[str, Any]):
            stats = member_stats.get(member["name"], {})
            shifts = stats.get("shifts_completed", 0)
            last_end = stats.get("last_shift_end") or "1970-01-01"
            return (shifts, last_end)

        available.sort(key=sort_key)
        selected = available[0]
        logger.info(f"Backup ausgewählt: {selected['name']} (Runner-Up).")
        return selected, "Runner-Up als Backup ausgewählt."

    def resolve_shifts(self) -> Tuple[Dict[str, Any], Dict[str, Any], bool, List[Dict[str, Any]]]:
        """
        Determines current active shift (Primary + Backup) and previews next week's shift.
        Returns: (current_shift, next_preview, is_handoff_day, calendar_events)
        """
        team = [m for m in self.config.get("team", []) if m.get("active", True)]
        if not team:
            raise ValueError("Keine aktiven Teammitglieder in 'team' konfiguriert!")

        shift_start, shift_end = self._calculate_shift_window(self.now)
        next_shift_start = shift_end
        next_shift_end = next_shift_start + dt.timedelta(days=self.config.get("rotation", {}).get("shift_duration_days", 7))

        calendar_events = self.fetch_calendar_events(shift_start, next_shift_end)

        current_shift = self.state.get("current_shift")
        is_handoff_day = False

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
            shift_start_hour = self.config.get("rotation", {}).get("shift_start_hour", 6)
            if self.now.date() >= curr_end_date:
                needs_rotation = True
                is_handoff_day = True
            elif self.now.date() == shift_start and self.now.hour >= shift_start_hour:
                if current_shift.get("start_date") != shift_start.isoformat():
                    needs_rotation = True
                    is_handoff_day = True

        if needs_rotation:
            logger.info(f"Neuer Schichtwechsel für Fenster {shift_start} bis {shift_end}...")
            if self.calendar_unreachable:
                # Fallback: Confluence unreachable
                primary_candidate, backup_candidate, note = self.pick_fallback_candidates(
                    team, "Confluence-Kalender war nicht erreichbar"
                )
                backup_note = note
            else:
                primary_candidate, primary_note = self.pick_fair_candidate(team, calendar_events, shift_start, shift_end)
                if not primary_candidate:
                    # Fallback: No candidate available (all on leave)
                    primary_candidate, backup_candidate, note = self.pick_fallback_candidates(
                        team, "Alle Teammitglieder abwesend"
                    )
                    backup_note = note
                else:
                    note = primary_note
                    next_primary, _ = self.pick_fair_candidate(team, calendar_events, next_shift_start, next_shift_end)
                    backup_candidate, backup_note = self.pick_backup_candidate(
                        team, primary_candidate, next_primary, calendar_events, shift_start, shift_end
                    )
                    if not backup_candidate:
                        # Fallback: Pick random backup from remaining pool
                        pool = [m for m in team if m["name"] != primary_candidate["name"]]
                        backup_candidate = random.choice(pool) if pool else primary_candidate
                        backup_note = "⚠️ Zufallsauswahl (Kein reguläres Backup verfügbar)"
                        self.fallback_triggered = True

            # Update member stats for primary
            name = primary_candidate["name"]
            stats = self.state["member_stats"].setdefault(name, {"shifts_completed": 0, "last_shift_end": None})
            stats["shifts_completed"] += 1
            stats["last_shift_end"] = shift_end.isoformat()

            current_shift = {
                "engineer": name,
                "email": primary_candidate.get("teams_email", ""),
                "backup": backup_candidate["name"],
                "backup_email": backup_candidate.get("teams_email", ""),
                "start_date": shift_start.isoformat(),
                "end_date": shift_end.isoformat(),
                "assigned_at": self.now.isoformat(),
                "note": note,
                "backup_note": backup_note,
            }
            self.state["current_shift"] = current_shift
            self.state.setdefault("history", []).append(current_shift)
            self._save_state()
        else:
            if "backup" not in current_shift or not current_shift["backup"]:
                logger.info("Schicht hat noch kein Backup. Ergänze Backup...")
                primary_name = current_shift["engineer"]
                primary_cand = next((m for m in team if m["name"] == primary_name), {"name": primary_name})
                next_primary, _ = self.pick_fair_candidate(team, calendar_events, next_shift_start, next_shift_end)
                backup_cand, b_note = self.pick_backup_candidate(
                    team, primary_cand, next_primary, calendar_events, shift_start, shift_end
                )
                if not backup_cand:
                    pool = [m for m in team if m["name"] != primary_name]
                    backup_cand = random.choice(pool) if pool else primary_cand
                    b_note = "⚠️ Zufallsauswahl"
                    self.fallback_triggered = True
                current_shift["backup"] = backup_cand["name"]
                current_shift["backup_email"] = backup_cand.get("teams_email", "")
                current_shift["backup_note"] = b_note
                self.state["current_shift"] = current_shift
                self._save_state()

            logger.info(
                f"Aktive Schicht: Primär={current_shift['engineer']}, "
                f"Backup={current_shift['backup']} (bis {current_shift['end_date']})"
            )

        # Pre-calculate preview for next week
        # Invariant: If current_shift has a valid backup who is not the current primary,
        # that backup is scheduled to be next week's primary!
        current_backup_name = current_shift.get("backup")
        current_primary_name = current_shift.get("engineer")
        designated_next_primary = None
        if current_backup_name and current_backup_name != current_primary_name:
            designated_next_primary = next((m for m in team if m["name"] == current_backup_name), None)

        if self.calendar_unreachable:
            if designated_next_primary:
                next_primary = designated_next_primary
                # Pick backup from remaining team members excluding next_primary
                rem_pool = [m for m in team if m["name"] != next_primary["name"]]
                next_backup = random.choice(rem_pool) if rem_pool else next_primary
                prev_note = "⚠️ Zufallsauswahl (Confluence-Kalender war nicht erreichbar)"
            else:
                next_primary, next_backup, prev_note = self.pick_fallback_candidates(
                    team, "Confluence-Kalender war nicht erreichbar"
                )
        else:
            if designated_next_primary:
                next_primary = designated_next_primary
                prev_note = ""
            else:
                # Exclude current primary from being picked directly again next week if pool permits
                cand_pool = [m for m in team if m["name"] != current_primary_name] if len(team) > 1 else team
                next_primary, next_primary_note = self.pick_fair_candidate(
                    cand_pool, calendar_events, next_shift_start, next_shift_end
                )
                prev_note = next_primary_note or ""

            if not next_primary:
                next_primary, next_backup, prev_note = self.pick_fallback_candidates(
                    team, "Alle Mitglieder in Folgewoche abwesend"
                )
            else:
                week_after_start = next_shift_end
                week_after_end = week_after_start + dt.timedelta(days=self.config.get("rotation", {}).get("shift_duration_days", 7))
                # Preview primary for 2 weeks ahead (excluding next_primary)
                cand_pool_after = [m for m in team if m["name"] != next_primary["name"]] if len(team) > 1 else team
                week_after_primary, _ = self.pick_fair_candidate(cand_pool_after, calendar_events, week_after_start, week_after_end)
                next_backup, _ = self.pick_backup_candidate(
                    team, next_primary, week_after_primary, calendar_events, next_shift_start, next_shift_end
                )
                if not next_backup or next_backup["name"] == next_primary["name"]:
                    pool = [m for m in team if m["name"] != next_primary["name"]]
                    next_backup = random.choice(pool) if pool else next_primary

        next_preview = {
            "engineer": next_primary["name"],
            "email": next_primary.get("teams_email", ""),
            "backup": next_backup["name"],
            "backup_email": next_backup.get("teams_email", ""),
            "start_date": next_shift_start.isoformat(),
            "end_date": next_shift_end.isoformat(),
            "note": prev_note,
        }

        return current_shift, next_preview, is_handoff_day, calendar_events

    # =========================================================================
    # Microsoft Teams Notification (Mondays & Fridays)
    # =========================================================================

    def build_teams_adaptive_card(
        self,
        current_shift: Dict[str, Any],
        next_preview: Dict[str, Any],
        is_handoff_day: bool,
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Builds a rich Adaptive Card JSON payload in German.
        Formatted specifically for:
        - Mondays: Schichtwechsel
        - Fridays: Forecast (Weekend & upcoming week preview)
        """
        teams_conf = self.config.get("teams", {})
        confluence_conf = self.config.get("confluence", {})
        cal_url = confluence_conf.get("calendar_page_url", "")
        betriebsaufgaben_url = teams_conf.get("betriebsaufgaben_url") or teams_conf.get("runbook_url", "")

        GERMAN_WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
        GERMAN_MONTHS = ["", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
        GERMAN_MONTHS_SHORT = ["", "Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."]

        weekday = self.now.weekday()  # 0 = Monday, 4 = Friday
        is_monday = (weekday == 0)
        is_friday = (weekday == 4)

        weekday_name = GERMAN_WEEKDAYS[weekday]
        month_name = GERMAN_MONTHS[self.now.month]
        today_str = f"{weekday_name}, {self.now.day:02d}. {month_name} {self.now.year}"

        curr_start_dt = dt.date.fromisoformat(current_shift["start_date"])
        # In reality, Betriebsaufgaben are active during the work week (Monday to Friday)
        curr_work_end_dt = curr_start_dt + dt.timedelta(days=4)
        next_start_dt = dt.date.fromisoformat(next_preview["start_date"])
        next_work_end_dt = next_start_dt + dt.timedelta(days=4)

        curr_start = f"{curr_start_dt.day:02d}. {GERMAN_MONTHS_SHORT[curr_start_dt.month]}"
        curr_work_end = f"{curr_work_end_dt.day:02d}. {GERMAN_MONTHS_SHORT[curr_work_end_dt.month]} {curr_work_end_dt.year}"
        next_start = f"{next_start_dt.day:02d}. {GERMAN_MONTHS_SHORT[next_start_dt.month]}"
        next_work_end = f"{next_work_end_dt.day:02d}. {GERMAN_MONTHS_SHORT[next_work_end_dt.month]} {next_work_end_dt.year}"

        curr_eng = current_shift["engineer"]
        curr_email = current_shift.get("email", "")
        curr_backup = current_shift.get("backup", "Keiner")
        curr_backup_email = current_shift.get("backup_email", "")

        next_eng = next_preview["engineer"]
        next_email = next_preview.get("email", "")
        next_backup = next_preview.get("backup", "Keiner")
        next_backup_email = next_preview.get("backup_email", "")

        # Default string without mention (Name + Email)
        primary_str = f"**{curr_eng}** ({curr_email})"
        backup_str = f"**{curr_backup}** ({curr_backup_email})"
        next_primary_str = f"**{next_eng}** ({next_email})"
        next_backup_str = f"**{next_backup}** ({next_backup_email})"

        # Mention helper: returns <at>Name</at> and entity if email is present
        def make_mention(name: str, email: str) -> Tuple[str, Optional[Dict[str, Any]]]:
            if not email or name in ("Keiner", "Niemand", "None"):
                return f"**{name}**", None
            tag = f"<at>{name}</at>"
            entity = {
                "type": "mention",
                "text": tag,
                "mentioned": {
                    "id": email,
                    "name": name,
                },
            }
            return tag, entity

        mention_entities: List[Dict[str, Any]] = []

        effective_mode = (self.card_type or "auto").lower()
        if effective_mode == "vorschau" or (effective_mode == "auto" and is_friday):
            title = "🔮 SRE Betriebsaufgaben: Vorschau"
            subtitle = "Vorschau auf die Folgewoche"
            accent_color = "accent"

            # Friday / Vorschau: Mention next week's Primary and Backup
            tag_next_p, ent_next_p = make_mention(next_eng, next_email)
            tag_next_b, ent_next_b = make_mention(next_backup, next_backup_email)
            if ent_next_p:
                mention_entities.append(ent_next_p)
            if ent_next_b and next_backup != next_eng:
                mention_entities.append(ent_next_b)

            facts = [
                {"title": "Datum:", "value": today_str},
                {"title": "Aktuell zuständig:", "value": primary_str},
                {"title": "Aktuell im Backup:", "value": backup_str},
                {"title": "Schichtende aktuell:", "value": f"Freitag, {curr_work_end}"},
                {"title": "Nächste Woche zuständig:", "value": tag_next_p},
                {"title": "Nächste Woche im Backup:", "value": tag_next_b},
                {"title": "Nächste Schicht:", "value": f"{next_start} – {next_work_end}"},
            ]
            vac_header = "**🌴 Geplante Abwesenheiten für die Folgewoche**"
            # In Friday forecast, show vacations for the upcoming week
            shift_start_filter = next_start_dt
            shift_end_filter = next_work_end_dt
        elif effective_mode == "schichtwechsel" or (effective_mode == "auto" and (is_monday or is_handoff_day)):
            title = "🔄 SRE Betriebsaufgaben: Schichtwechsel"
            subtitle = "Wöchentliche Schichtübergabe"
            accent_color = "warning"

            # Monday / Schichtwechsel: Mention current week's Primary and Backup
            tag_curr_p, ent_curr_p = make_mention(curr_eng, curr_email)
            tag_curr_b, ent_curr_b = make_mention(curr_backup, curr_backup_email)
            if ent_curr_p:
                mention_entities.append(ent_curr_p)
            if ent_curr_b and curr_backup != curr_eng:
                mention_entities.append(ent_curr_b)

            facts = [
                {"title": "Datum:", "value": today_str},
                {"title": "Primär:", "value": tag_curr_p},
                {"title": "Backup:", "value": tag_curr_b},
                {"title": "Aktuelle Schicht:", "value": f"{curr_start} – {curr_work_end}"},
                {"title": "Folgewoche (Vorschau):", "value": f"{next_primary_str} | {next_backup_str}"},
            ]
            vac_header = "**🌴 Abwesenheiten & Urlaub im Team**"
            shift_start_filter = curr_start_dt
            shift_end_filter = curr_work_end_dt
        else:
            title = "☀️ SRE Betriebsaufgaben: Übersicht"
            subtitle = "Aktuelle Zuständigkeiten"
            accent_color = "default"
            facts = [
                {"title": "Datum:", "value": today_str},
                {"title": "Primär:", "value": primary_str},
                {"title": "Backup:", "value": backup_str},
                {"title": "Aktuelle Schicht:", "value": f"{curr_start} – {curr_work_end}"},
                {"title": "Folgewoche (Vorschau):", "value": f"{next_primary_str} | {next_backup_str}"},
            ]
            vac_header = "**🌴 Abwesenheiten & Urlaub im Team**"
            shift_start_filter = curr_start_dt
            shift_end_filter = curr_work_end_dt

        # Filter leave events for the relevant window: ONLY include real leaves/vacations/travel
        vac_conf = self.config.get("vacation", {})
        default_keywords = ["leave", "leave (planned)", "travel", "urlaub", "vacation", "abwesend", "pto", "holiday"]
        event_keywords = [k.lower() for k in vac_conf.get("event_keywords", default_keywords)]
        default_ignores = ["birthday", "geburtstag", "meeting", "retro", "intern", "daily", "refinement", "pi ", "quarter cap"]
        ignore_keywords = [k.lower() for k in vac_conf.get("ignore_keywords", default_ignores)]

        vacations_now = []
        for ev in events:
            summary = ev.get("summary", "").lower()
            categories = [c.lower() for c in ev.get("categories", [])]
            combined_text = f"{summary} {' '.join(categories)}"

            # Exclude meetings, standups, birthdays, PI banners
            if any(ign in combined_text for ign in ignore_keywords):
                continue

            # Must match leave/vacation/travel keywords
            if not any(kw in combined_text for kw in event_keywords):
                continue

            if max(ev["start"], shift_start_filter) <= min(ev["end"], shift_end_filter):
                ev_s = ev["start"]
                ev_e = ev["end"]

                raw_summary = ev.get("summary", "Urlaub").strip()
                # Find matching team member from config or attendees
                matched_member = None
                for m in self.config.get("team", []):
                    patterns = [m["name"].lower()] + [str(a).lower() for a in m.get("aliases", [])]
                    event_ident_text = f"{raw_summary} {ev.get('description', '')} {' '.join(categories)} {' '.join(ev.get('attendees', []))}".lower()
                    if any(p in event_ident_text for p in patterns):
                        matched_member = m
                        break

                if matched_member:
                    member_name = matched_member["name"]
                    # If member name or aliases are not in the summary, prepend the person's name
                    already_has_name = any(
                        p in raw_summary.lower()
                        for p in [member_name.lower()] + [str(a).lower() for a in matched_member.get("aliases", [])]
                    )
                    if not already_has_name:
                        display_text = f"{member_name}: {raw_summary}"
                    else:
                        display_text = raw_summary
                elif ev.get("attendees"):
                    att = ev["attendees"][0]
                    display_text = f"{att}: {raw_summary}"
                else:
                    display_text = raw_summary

                # Format single-day vs multi-day nicely
                if ev_s == ev_e:
                    date_str = f"{ev_s.day:02d}.{ev_s.month:02d}.{ev_s.year}"
                elif ev_s.year == ev_e.year:
                    date_str = f"{ev_s.day:02d}.{ev_s.month:02d}. bis {ev_e.day:02d}.{ev_e.month:02d}.{ev_e.year}"
                else:
                    date_str = f"{ev_s.day:02d}.{ev_s.month:02d}.{ev_s.year} bis {ev_e.day:02d}.{ev_e.month:02d}.{ev_e.year}"

                vacations_now.append(f"• {display_text} ({date_str})")

        vacation_text = "\n".join(vacations_now) if vacations_now else "Keine eingetragenen Abwesenheiten für diesen Zeitraum."

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
                        "text": subtitle,
                        "isSubtle": True,
                        "spacing": "None",
                        "wrap": True,
                    },
                ],
            },
        ]

        # Warning container if random fallback was triggered
        is_fallback = (
            self.calendar_unreachable
            or self.fallback_triggered
            or "Zufallsauswahl" in str(current_shift.get("note", ""))
            or "Zufallsauswahl" in str(current_shift.get("backup_note", ""))
        )
        if is_fallback:
            body.append({
                "type": "Container",
                "style": "attention",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "⚠️ **Achtung: Zufallsauswahl aktiv!**",
                        "color": "Attention",
                        "weight": "Bolder",
                    },
                    {
                        "type": "TextBlock",
                        "text": "Der Confluence-Kalender war nicht erreichbar oder es konnte keine reguläre Person ermittelt werden. Die Zuweisung erfolgte per Zufallsauswahl aus dem Team.",
                        "wrap": True,
                    },
                ],
            })

        body.extend([
            {
                "type": "FactSet",
                "facts": facts,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": vac_header,
                "weight": "Bolder",
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": vacation_text,
                "wrap": True,
                "isSubtle": True,
            },
        ])

        if effective_mode == "vorschau" or (effective_mode == "auto" and is_friday):
            body.append({
                "type": "TextBlock",
                "text": "💡 *Bitte prüft eure Verfügbarkeit für nächste Woche. Falls ein Schichttausch nötig ist, sprecht euch bitte rechtzeitig ab.*",
                "wrap": True,
                "isSubtle": True,
            })

        actions = []
        if cal_url:
            actions.append({
                "type": "Action.OpenUrl",
                "title": "📅 SRE-Kalender öffnen",
                "url": cal_url,
            })
        if betriebsaufgaben_url:
            actions.append({
                "type": "Action.OpenUrl",
                "title": "📋 Betriebsaufgaben öffnen",
                "url": betriebsaufgaben_url,
            })

        adaptive_card_content: Dict[str, Any] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "type": "AdaptiveCard",
            "body": body,
            "actions": actions,
        }
        if mention_entities:
            adaptive_card_content["msteams"] = {
                "entities": mention_entities
            }

        card_payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": adaptive_card_content,
                }
            ],
        }
        return card_payload

    def notify_teams(self, card_payload: Dict[str, Any]) -> bool:
        webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip().strip("'\"")
        # Automatically clean shell escape backslashes (e.g. from zsh bracketed paste: \?, \&, \=)
        webhook_url = webhook_url.replace(r"\?", "?").replace(r"\&", "&").replace(r"\=", "=")

        if self.dry_run or not webhook_url:
            logger.info("[Dry Run or Missing TEAMS_WEBHOOK_URL] Teams card payload preview:")
            print(json.dumps(card_payload, indent=2))
            return True

        if ("powerplatform.com" in webhook_url or "logic.azure.com" in webhook_url) and "api-version=" not in webhook_url:
            sep = "&" if "?" in webhook_url else "?"
            logger.warning("TEAMS_WEBHOOK_URL is missing 'api-version'. Auto-appending 'api-version=2024-10-01'.")
            webhook_url = f"{webhook_url}{sep}api-version=2024-10-01"

        try:
            logger.info("Posting Adaptive Card to Microsoft Teams...")
            if requests is not None:
                resp = requests.post(webhook_url, json=card_payload, timeout=10)
                if resp.status_code in (200, 202):
                    logger.info("Successfully posted on-call notification to Microsoft Teams.")
                    return True

                if resp.status_code == 400 and "attachments" in card_payload:
                    logger.info("Retrying with unwrapped raw AdaptiveCard schema...")
                    raw_card = card_payload["attachments"][0]["content"]
                    retry_resp = requests.post(webhook_url, json=raw_card, timeout=10)
                    if retry_resp.status_code in (200, 202):
                        logger.info("Successfully posted on-call notification to Microsoft Teams (unwrapped).")
                        return True
                    logger.error(f"Failed retry. Status {retry_resp.status_code}: {retry_resp.text}")

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
        "--card-type",
        type=str,
        default="auto",
        choices=["auto", "uebersicht", "schichtwechsel", "vorschau"],
        help="Notification template: auto (weekday-based), uebersicht, schichtwechsel, or vorschau.",
    )
    parser.add_argument(
        "--current-time",
        type=str,
        default=None,
        help="Simulate running at a specific ISO datetime (e.g. 2026-11-09T06:05:00).",
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
        card_type=args.card_type,
        simulated_now=simulated_now,
    )
    sys.exit(manager.run())


if __name__ == "__main__":
    main()
