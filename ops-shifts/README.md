# SRE Ops Shifts & Teams Notifier

Automated on-call shift rotation, vacation conflict avoidance via Confluence, and Microsoft Teams notifications for the SRE team.

## Overview

- **Weekly Rotation & Betriebsaufgaben (Mo–Fr)**:
  - Shifts rotate every **Monday at 06:00 CET/CEST** (Europe/Berlin) and cover SRE operational duties (Betriebsaufgaben: Alerting-Channels, technische Boardings, etc.) during regular working hours (Monday–Friday).
  - Teams notifications are posted twice weekly at **06:00 CET/CEST**:
    - **Mondays (Schichtwechsel)**: Introduces the new Primary and Backup engineers for the work week.
    - **Fridays (Wochenausklang & Vorschau)**: Summarizes the week's end and gives an early preview of the upcoming Monday shift assignments and leaves.
- **Two Roles Per Shift (Primary + Backup)**:
  - **Primary On-Call**: Selected via the fair *least-shifts-completed* algorithm among engineers not on leave.
  - **Backup On-Call**: Previews and assigns the engineer scheduled for the following week (or the next available runner-up).
  - *Fairness Guarantee*: Serving as backup does **not** increment primary `shifts_completed`.
- **Vacation & Travel Conflict Avoidance (Workdays Only)**:
  - Ingests the shared Confluence SRE Calendar:
    `https://docs.dbildungscloud.de/display/DBH/calendar/60fe65e8-fcac-4358-bf52-e7188a5fc1c3?calendarName=SRE`
  - **Weekends Excluded**: Saturday and Sunday are automatically excluded from absence calculations since Betriebsaufgaben only occur during working days (Monday–Friday). A weekend-only absence has zero impact on shift eligibility!
  - Explicitly recognizes **`Leave`**, **`Leave (planned)`**, and **`Travel`** (and German *Urlaub*, *Abwesend*).
  - Automatically ignores non-absence calendar banners (e.g. **`Birthdays`**, `PI`, `Daily`, `Refinement`, `Quarter Cap`).
- **Automatic Fallback Mechanism**:
  - If Confluence is unreachable (network/API error) or if no eligible candidate is found (e.g. all members on leave), the script falls back to assigning 2 distinct active team members at random.
  - An attention banner is displayed on the Teams card alerting the team that a fallback assignment was used.
- **Separation of Concerns: State Persistence (Zero Git Commits!)**:
  - **Caller-Managed (`gh variable get / set`)**: The Python application operates purely on a local state file (`state.json`) and is decoupled from any cloud or CI API.
  - In GitHub Actions, the workflow runner fetches and updates the repository variable `ONCALL_STATE` using the native GitHub CLI (`gh`). No git commits, no git push, and no commit log pollution!

---

## Directory Structure

```
ops-shifts/
├── config.yaml            # Team roster, calendar ID, vacation keywords, rotation parameters
├── ops_shifts.py          # Main execution script
├── requirements.txt       # Dependencies (icalendar, requests, PyYAML, etc.)
├── state.json             # Local fallback state store
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
  betriebsaufgaben_url: "https://docs.dbildungscloud.de/spaces/ST/pages/534937640/SRE+Betriebsaufgaben+OPS"

rotation:
  timezone: "Europe/Berlin"
  shift_duration_days: 7
  shift_start_weekday: 0   # 0 = Monday
  shift_start_hour: 6      # 06:00 Europe/Berlin (CET/CEST)
  vacation_overlap_threshold_days: 1  # Mindestanzahl an abwesenden Arbeitstagen (Mo-Fr)

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
    - "holidays"
  ignore_keywords:
    - "birthday"
    - "geburtstag"
    - "meeting"
    - "retro"
    - "refinement"
    - "daily"
    - "intern"
    - "quarter cap"
    - "pi "

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

- **Schedule**: Evaluated on **Mondays** and **Fridays** at `05:00 UTC` (06:00 CET).
- **Permissions**:
  ```yaml
  permissions:
    contents: read
    variables: write
  ```
  Enables updating the `ONCALL_STATE` repository variable without any git commits.

### Required GitHub Secrets & Variables

In repository settings under **Settings → Secrets and variables → Actions**:

| Name | Type | Description |
| :--- | :--- | :--- |
| `CONFLUENCE_PAT` | Secret | Personal Access Token with read access to the SRE calendar |
| `TEAMS_WEBHOOK_URL` | Secret | Full Microsoft Teams Workflows webhook URL (with `api-version` and `sig`) |
| `ONCALL_STATE` | Variable | (Optional) Automatically initialized/updated by the runner |

---

## Local Development & Testing

```bash
# Run unit test suite
python3 -m unittest ops-shifts/test_ops_shifts.py

# Simulate Monday morning Schichtwechsel (dry-run)
python3 ops-shifts/ops_shifts.py --dry-run --mock-calendar --current-time 2026-11-09T06:00:00

# Simulate Friday morning Forecast (dry-run)
python3 ops-shifts/ops_shifts.py --dry-run --mock-calendar --current-time 2026-11-06T06:00:00

# Live run with real credentials
export CONFLUENCE_PAT="your-token"
export TEAMS_WEBHOOK_URL='https://...'
python3 ops-shifts/ops_shifts.py
```
