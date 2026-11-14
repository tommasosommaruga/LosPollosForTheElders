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

1. Arm PC: `medicine-buddy serve --host 0.0.0.0 --port 8787`
2. Reachy PC `.env`:
   - `MEDICINE_BUDDY_URL=http://<arm-pc-lan-ip>:8787`
   - `REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY=.../integrations/reachy_conversation/external_tools`
   - `REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY=.../integrations/reachy_conversation/external_profiles`
   - `REACHY_MINI_CUSTOM_PROFILE=medicine_caregiver`
   - `AUTOLOAD_EXTERNAL_TOOLS=1`
3. Start `reachy_mini_conversation_app` on the Reachy PC

Details: `integrations/reachy_conversation/README.md`

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
