---
name: meds-dispense-pipeline
description: Run the dose state machine from Due through place-verify. Use when changing dispense flow, transitions, or orchestration wiring.
---

# Meds dispense pipeline

## States

`Due → Reminded → Confirmed → Locating → Picking → Placed → Watching → Taken|Missed|Failed`

## Gates

1. Person confirm before locate/pick
2. OCR confidence before pick
3. Arm-cam place verify before Watching

## Code

- `orchestrator.py` (`DoseStateMachine`)
- Persist every transition via `db.record_transition`
