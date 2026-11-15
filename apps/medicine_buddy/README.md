---
title: Medicine Buddy
emoji: 💊
colorFrom: green
colorTo: yellow
sdk: static
pinned: false
short_description: Medicine reminders per person and family memories, protected by spoken passphrases
tags:
 - reachy_mini
 - reachy_mini_python_app
---

# Medicine Buddy (Reachy Mini app + Python package)

A Reachy Mini app for elderly care: per-person medicine reminders, a family memory store
(stories, recipes, skills, traditions) and passphrase access control - plus the dose
pipeline (remind → confirm → OCR locate → arm place → adherence) for the Maker Arm.

Full project, setup and run instructions: **[../../README.md](../../README.md)**  
Arm contract detail: **[../../docs/ARM_TEAM_HANDOFF.md](../../docs/ARM_TEAM_HANDOFF.md)**

## Commands

```bash
pip install -e ".[dev]"                         # also installs reachy-mini + the conversation app
python -m medicine_buddy.main --host <robot-ip> # the whole app, from a laptop
medicine-buddy serve --dry-run --voice-guard    # only the API / status page
medicine-buddy demo --dry-run                   # console demo of the dose pipeline
pytest
```

## Module cheat sheet

| File | Role |
|------|------|
| `main.py` | **Reachy Mini app** (`MedicineBuddy`): conversation app + API + voice guard |
| `conversation/` | Voice tools and the `medicine_caregiver` profile loaded by the conversation app |
| `api.py` | HTTP API, status page `/`, family settings `/family` |
| `web_ui.py` | In-app `/medicine` pages (dashboard, history, family, debug) + session auth |
| `history_page.py` / `explorer_api.py` | History explorer UI + auth-gated `/medicine/api/*` |
| `access.py` | People, passphrases, access control, family memories |
| `settings.py` / `family_page.py` | Typed passphrase changes (never spoken) |
| `debug.py` | Request activity (RAM + SQLite `activity_events`) and debug snapshot |
| `voice_guard.py` | Medicine changes only when the person really said so |
| `web_safety.py` / `catalog.py` | Web result cleaning; allowed medicine names |
| `orchestrator.py` | `DoseStateMachine` |
| `ports.py` / `dry_run.py` | Arm / vision contracts and fakes |
| `db.py` / `states.py` | Persistence + legal transitions |

## States

`Idle → Due → Reminded → Confirmed → Locating → Picking → Placed → Watching → Taken | Missed | Failed`
