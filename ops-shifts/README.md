# SRE Ops Shifts & Teams Notifier

Automated on-call shift rotation and daily Microsoft Teams notification bot for the SRE team.

## Overview

- **Weekly Rotation**: Rotates on-call shifts every Monday morning.
- **Fair Scheduling**: Uses a *least-shifts-completed* algorithm with tie-breaking based on longest time since last shift.
- **Vacation Conflict Avoidance**: Reads the team's shared vacation calendar on Confluence (`https://docs.dbildungscloud.de/display/DBH/calendar/60fe65e8-fcac-4358-bf52-e7188a5fc1c3?calendarName=SRE`) and automatically skips members who are on leave during an upcoming shift.
- **Daily Teams Notification**: Posts an Adaptive Card every weekday morning summarizing who is on call, active shift dates, upcoming on-call engineer for next week, and quick links to runbooks and the Confluence calendar.
- **Self-Balancing**: When an engineer returns from vacation, their shift count remains lower, ensuring they naturally pick up upcoming shifts without manual debt tracking.

---

## Directory Structure

```
ops-shifts/
├── config.yaml            # Team members, calendar ID, timezone, rotation preferences
├── ops_shifts.py          # Main execution script
├── requirements.txt       # Python dependencies (icalendar, requests, PyYAML, etc.)
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
  calendar_page_url: "https://docs.dbildungscloud.de/display/DBH/calendar/60fe65e8-fcac-4358-bf52-e7188a5fc1c3?calendarName=SRE"

teams:
  channel_name: "SRE Operations"
  runbook_url: "https://docs.dbildungscloud.de/display/PROD/SRE+Runbooks"

rotation:
  timezone: "Europe/Berlin"
  shift_duration_days: 7
  shift_start_weekday: 0   # 0 = Monday
  shift_start_hour: 9      # 09:00 Europe/Berlin
  vacation_overlap_threshold_days: 1

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

Configure these in repository settings (`Settings -> Secrets and variables -> Actions`):

| Secret Name | Description |
| :--- | :--- |
| `CONFLUENCE_PAT` | Confluence Personal Access Token with read access to the SRE calendar |
| `TEAMS_WEBHOOK_URL` | Microsoft Teams incoming webhook or Power Automate workflow URL |

### Schedule & Manual Dispatch
- **Schedule**: Evaluated Mon–Fri at `07:30 UTC` (08:30 CET / 09:30 CEST).
- **Manual Trigger**: Can be executed manually via the GitHub Actions **Run workflow** button with options for `dry_run`, `force_rotation`, and `mock_calendar`.

---

## Local Development & Testing

### 1. Install dependencies
```bash
pip install -r ops-shifts/requirements.txt
```

### 2. Run unit tests
```bash
python -m unittest ops-shifts/test_ops_shifts.py
```

### 3. Run dry-run with mock calendar
```bash
python ops-shifts/ops_shifts.py --dry-run --mock-calendar
```

### 4. Run dry-run with live Confluence calendar
```bash
export CONFLUENCE_PAT="your-token"
python ops-shifts/ops_shifts.py --dry-run
```

---

## Manual Overrides & Swapping Shifts

If two team members want to swap shifts or if an emergency change is needed, simply edit `ops-shifts/state.json`:
- Change `"engineer"` under `"current_shift"` to the new person.
- Adjust `"shifts_completed"` in `"member_stats"` if desired.
- Commit and push to git. The notifier will immediately use the updated state on the next scheduled run.
