# Medicine Buddy — Agent Guide

Laptop-orchestrated elderly-care MVP: **Reachy Mini** (voice, remind, adherence) + **Maker Arm** (OCR + taught pick-place into a bowl).

**Read first for humans/agents:** [README.md](README.md) (vision, architecture, roadmap) and [docs/ARM_TEAM_HANDOFF.md](docs/ARM_TEAM_HANDOFF.md) (ports for the arm/camera team).

## FIRST: Check agents.local.md

```
IF agents.local.md exists in repo root:
    Read it immediately (robot type, camera indices, dry_run)
ELSE:
    Copy agents.local.md.example → agents.local.md
    Prefer dry_run: true until hardware is verified
```

## Always / Ask-first / Never

**Always**
- Confirm with the person before any arm dispense move
- Require OCR confidence above threshold before motion
- Persist every dose state transition to SQLite
- Prefer dry-run ports when hardware is unavailable

**Ask-first**
- Live motor power / first hardware move of a session
- Changing taught waypoints or tray slot map
- Anything that looks like medical dosing advice

**Never**
- Open packaging or count pills
- Give medical / dosing advice
- Move the arm on low OCR confidence
- Lift people or handle hot liquids / sharps

## App location

Primary code: `apps/medicine_buddy/`

Before non-trivial feature work, update `apps/medicine_buddy/plan.md` and wait for user answers when questions are listed there.

## Skills

| Skill | When |
|-------|------|
| [setup-environment](.cursor/skills/setup-environment/SKILL.md) | First session, venv, doctors |
| [reachy-connect-voice](.cursor/skills/reachy-connect-voice/SKILL.md) | Reachy speech / confirm prompts |
| [medicine-schedule-db](.cursor/skills/medicine-schedule-db/SKILL.md) | Medicines, schedules, dose_events |
| [arm-connect-control](.cursor/skills/arm-connect-control/SKILL.md) | Maker Arm teach/replay |
| [medicine-vision-ocr](.cursor/skills/medicine-vision-ocr/SKILL.md) | Dual-cam labels + confidence |
| [meds-dispense-pipeline](.cursor/skills/meds-dispense-pipeline/SKILL.md) | Dose state machine end-to-end |
| [adherence-watch](.cursor/skills/adherence-watch/SKILL.md) | Cam + voice fallback taken/missed |

## Dose states

`Idle → Due → Reminded → Confirmed → Locating → Picking → Placed → Watching → Taken | Missed | Failed`

## Quick dry-run

```bash
cd apps/medicine_buddy
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
medicine-buddy demo --dry-run
```

## Robot host PC (pull and run)

When the other computer is connected to Reachy and will `git pull` this repo:

```bash
./scripts/bootstrap.sh
./scripts/run_robot_host.sh                    # Medicine Buddy API
./scripts/run_conversation_with_medicine.sh    # pollen conversation TTS
```

Same-machine default: API at `http://127.0.0.1:8787`, tools path auto-written to
`integrations/reachy_conversation/.env.generated`.

Full steps: [integrations/reachy_conversation/README.md](integrations/reachy_conversation/README.md)

## References

- Reachy Mini AGENTS: https://github.com/pollen-robotics/reachy_mini/blob/main/AGENTS.md
- Maker Arm: https://www.makermods.ai/maker-arm
- Project notes: `context.md`, `house-help-ideas.md`
