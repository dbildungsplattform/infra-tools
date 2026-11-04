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
  - **Caller-Managed (`actions/cache`)**: The Python application operates purely on a local state file (`ops-shifts/state.json`) and is decoupled from any cloud or CI API.
  - In GitHub Actions, the workflow runner persists and restores `state.json` across workflow runs via `actions/cache`. No git commits, no git push, and no commit log pollution!

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

## GitHub Actions Deployment

The workflow is located at [`.github/workflows/ops-shifts.yaml`](file:///home/akorzhyl/git/dataport-dbildung/infra-tools/.github/workflows/ops-shifts.yaml).

- **Schedule**: Evaluated on **Mondays** and **Fridays** at `05:00 UTC` (06:00 CET).
- **State Storage**: Uses `actions/cache` to persist `ops-shifts/state.json` seamlessly across workflow runs without repository commits or extra tokens.

### Required GitHub Secrets

In repository settings under **Settings → Secrets and variables → Actions → Secrets**:

| Name | Type | Description |
| :--- | :--- | :--- |
| `CONFLUENCE_PAT` | Secret | Personal Access Token with read access to the SRE calendar |
| `TEAMS_WEBHOOK_URL` | Secret | Full Microsoft Teams Workflows webhook URL (with `api-version` and `sig`) |

---

## Local Testing

```bash
# 1. Set environment variables (single quotes prevent URL-escaping issues)
export CONFLUENCE_PAT="<your-confluence-pat>"
export TEAMS_WEBHOOK_URL='<your-teams-webhook-url>'

# 2. Run unit tests
python3 -m unittest ops-shifts/test_ops_shifts.py

# 3. Dry-Run simulation (no Teams message, no state update)
python3 ops-shifts/ops_shifts.py --dry-run

# 4. Test different Teams card templates (live post)
python3 ops-shifts/ops_shifts.py --card-type uebersicht      # Default / Übersicht
python3 ops-shifts/ops_shifts.py --card-type schichtwechsel # Monday shift handover
python3 ops-shifts/ops_shifts.py --card-type vorschau       # Friday preview

# 5. Simulate forced rotation without saving
python3 ops-shifts/ops_shifts.py --force-rotation --dry-run
```
