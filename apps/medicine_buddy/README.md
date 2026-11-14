# medicine_buddy (Python package)

Orchestrator for elderly medicine remind → confirm → OCR locate → arm place → adherence.

Full project vision, architecture, and arm-team handoff: **[../../README.md](../../README.md)**  
Arm contract detail: **[../../docs/ARM_TEAM_HANDOFF.md](../../docs/ARM_TEAM_HANDOFF.md)**

## Commands

```bash
pip install -e ".[dev]"
medicine-buddy demo --dry-run
medicine-buddy serve --host 127.0.0.1 --port 8787 --dry-run
medicine-buddy check-due
pytest
```

## Module cheat sheet

| File | Role |
|------|------|
| `orchestrator.py` | `DoseStateMachine` |
| `api.py` | HTTP for pollen conversation tools |
| `ports.py` | `VisionPort` / `ArmPort` / … contracts |
| `dry_run.py` | Fake implementations |
| `db.py` / `states.py` | Persistence + legal transitions |
| `vision_ocr.py` / `arm_controller.py` | Hardware stubs for the arm team |

## States

`Idle → Due → Reminded → Confirmed → Locating → Picking → Placed → Watching → Taken | Missed | Failed`
