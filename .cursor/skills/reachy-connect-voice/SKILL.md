---
name: reachy-connect-voice
description: Wire Medicine Buddy to pollen reachy_mini_conversation_app for TTS/STT on a Reachy PC, with the arm PC serving the HTTP API. Use when another computer owns the robot or conversation TTS must stay working.
---

# Reachy connect + voice (conversation app)

## Topology

```text
Reachy PC: daemon + reachy_mini_conversation_app (TTS/STT/LLM)
Arm PC:    medicine-buddy serve :8787 (schedule, OCR, arm)
```

Do **not** reinvent TTS in Medicine Buddy when conversation app is available.

## Setup

The Reachy Mini app `medicine_buddy.main:MedicineBuddy` wires this up for you: it sets the
conversation app's env (tools + profile from `medicine_buddy/conversation/`, profile locked to
`medicine_caregiver`), runs the API in-process and feeds speech transcripts to the voice guard.

- Same machine: `python -m medicine_buddy.main --host <robot-ip>` (or on the robot via Reachy Mini Control).
- Arm on a second PC: run `medicine-buddy serve --host 0.0.0.0 --port 8787` there and set
  `MEDICINE_BUDDY_URL=http://<arm-pc-lan-ip>:8787` in `.env` where the app runs (note: the family page
  and `/voice/heard` only accept requests from the same machine).

Details: `README.md` → How to run

## Tools

| Tool | Role |
|------|------|
| `schedule_medicine` | Voice → schedule DB |
| `check_due_medicines` | Due now? |
| `dispense_medicine` | After yes → arm pick to bowl |
| `mark_dose_taken` | After "did you take it?" |

## Rules

- Confirm before `dispense_medicine(confirmed=true)`
- Never dosing advice
- Dry-run console buddy only when conversation app is unavailable
