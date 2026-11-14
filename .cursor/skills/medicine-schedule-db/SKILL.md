---
name: medicine-schedule-db
description: Manage medicines, recurrent schedules, and dose_events in SQLite. Use when adding schedule fields, voice setup persistence, or dose state history.
---

# Medicine schedule DB

## Tables

- `medicines(id, name, created_at)`
- `schedules(id, medicine_id, weekdays, time_hhmm, timezone, active)`
- `dose_events(id, medicine_id, state, slot_id, detail, created_at)`

## Voice setup flow

name → weekdays → time → confirm → persist

## Code

- `medicine_buddy/db.py`
- `medicine_buddy/voice_setup.py`
- `medicine_buddy/scheduler.py`
