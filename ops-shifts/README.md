# SRE Ops Shifts & Teams Notifier

Automated on-call shift rotation and daily Microsoft Teams notification bot for the SRE team.

## Overview

- **Weekly Rotation**: Rotates on-call shifts every Monday morning at 09:00 (Europe/Berlin).
- **Two Roles Per Shift (Primary + Backup)**:
  - **Primary On-Call**: Handled by the fair *least-shifts-completed* algorithm.
  - **Backup On-Call**: Previews and assigns the engineer scheduled for the following week (or the next available runner-up). If the primary falls ill or is occupied, the backup steps in.
  - *Fairness Guarantee*: Serving as backup does **not** count towards primary `shifts_completed`, keeping primary rotations completely fair and balanced.
- **Vacation & Absence Conflict Avoidance**:
  - Ingests the shared Confluence SRE Calendar:
    `https://docs.dbildungscloud.de/display/DBH/calendar/60fe65e8-fcac-4358-bf52-e7188a5fc1c3?calendarName=SRE`
  - Explicitly detects:
    - **`Leave`**
    - **`Leave (planned)`**
    - **`Travel`**
    - German/common terms like *Urlaub*, *Vacation*, *Abwesend*, *PTO*
  - Automatically ignores non-absence events on the shared calendar (e.g. **`Birthdays`**, meetings, etc.).
- **Daily Teams Notification**: Posts an Adaptive Card every weekday morning summarizing:
  - **Primary On-Call**: Current active engineer
  - **Backup On-Call**: Assigned secondary engineer
  - **Active Shift Window**: e.g., `Nov 02 – Nov 09`
  - **Next Up (Next Week)**: Preview of next week's Primary and Backup
  - **Team Leave & Travel Status**: Summary of team members currently absent
  - Direct links to the Confluence calendar and SRE runbooks.

---

## Directory Structure

```
ops-shifts/
├── config.yaml            # Team roster, calendar ID, vacation keywords, rotation parameters
├── ops_shifts.py          # Main execution script
├── requirements.txt       # Dependencies (icalendar, requests, PyYAML, etc.)
├── state.json             # Persistent rotation history & shift counters (managed via git)
├── test_ops_shifts.py     # Unit tests
└── README.md              # Documentation
```

---

## Configuration (`config.yaml`)

```yaml
confluence:
  base_url: "https://docs.dbildungscloud.de"
  calendar_id: "60fe65e8-fcac-4358-bf52-e7188a5fc1c3"
  calendar_name: "SRE"
  calendar_page_url: "https://docs.dbildungscloud.de/spaces/ST/overview"

teams:
  channel_name: "SRE Operations"
  runbook_url: "https://docs.dbildungscloud.de/spaces/ST/pages/534937640/SRE+Betriebsaufgaben+OPS"

rotation:
  timezone: "Europe/Berlin"
  shift_duration_days: 7
  shift_start_weekday: 0   # 0 = Monday
  shift_start_hour: 9      # 09:00 Europe/Berlin
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
  - name: "Alex Example"
    aliases: ["Alex", "Example", "aexample"]
    teams_email: "alex.example@dbildungscloud.de"
    active: true
  - name: "Jordan Sample"
    aliases: ["Jordan", "Sample", "jsample"]
    teams_email: "jordan.sample@dbildungscloud.de"
    active: true
```

---

## GitHub Actions Deployment

The workflow is located at [`.github/workflows/ops-shifts.yaml`](file:///home/akorzhyl/git/dataport-dbildung/infra-tools/.github/workflows/ops-shifts.yaml).

### Required GitHub Secrets

Configure in repository settings (`Settings -> Secrets and variables -> Actions`):

| Secret Name | Description |
| :--- | :--- |
| `CONFLUENCE_PAT` | Confluence Personal Access Token with read access to the SRE calendar |
| `TEAMS_WEBHOOK_URL` | Microsoft Teams incoming webhook or Power Automate workflow URL |

---

## Local Development & Testing

```bash
# Run unit test suite
python3 -m unittest ops-shifts/test_ops_shifts.py

# Run dry run with simulated calendar
python3 ops-shifts/ops_shifts.py --dry-run --mock-calendar

# Run dry run with live Confluence credentials
export CONFLUENCE_PAT="your-token"
python3 ops-shifts/ops_shifts.py --dry-run
```
