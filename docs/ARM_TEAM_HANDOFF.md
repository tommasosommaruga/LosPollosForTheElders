# Arm / camera / movement-training handoff

This document is for the subgroup working on **Maker Arm software, cameras, and motion training**. The orchestration repo is already runnable in dry-run; you replace fakes with real hardware.

## What already works without you

- Medicine schedule DB + due detection  
- Confirm → locate → pick → place → adherence **state machine**  
- HTTP API + Reachy conversation tools (TTS on pollen app)  
- `FakeVision` / `FakeArm` for demos and CI  

## What we need from you

Implement the ports in `apps/medicine_buddy/medicine_buddy/ports.py`:

### 1. `VisionPort.locate_medicine(medicine_name) -> OcrMatch | None`

- Use overhead (+ optional wrist) cameras.  
- Read unique text on standardized boxes.  
- Return `OcrMatch(slot_id, label, score)`.  
- Return `None` if empty, unknown, low score, or more than one match.  
- Known labels come from the schedule DB / tray config (`agents.local.md` / `AppConfig.tray_labels`).

### 2. `ArmPort.replay_slot_to_bowl(slot_id)`

- Replay a **taught** trajectory for that slot into the fixed bowl pose.  
- Prefer recorded waypoints over online planning for the hackathon.  
- Respect e-stop and joint limits.

### 3. `ArmPort.verify_place(slot_id) -> PlaceVerifyResult`

- After replay, confirm via cameras that the bowl looks occupied / slot cleared enough.  
- `ok=False` must abort the dose as `Failed` (orchestrator already does this).

## Integration steps

1. Keep developing against your SDK / Lab / training pipeline.  
2. Add a module e.g. `medicine_buddy/hardware_arm.py` + `hardware_vision.py`.  
3. In `api.py` / CLI, when `dry_run=false`, construct your classes instead of `Fake*`.  
4. Store machine-specific waypoints under `apps/medicine_buddy/data/waypoints/` (gitignored pattern already set).  
5. Document camera indices in `agents.local.md`.

## Demo success criteria

- [ ] Labeled box for due medicine is identified with confidence ≥ threshold  
- [ ] Arm places **closed** box in bowl without human teleop  
- [ ] Wrong/low-confidence label → no motion  
- [ ] Place failure → spoken/API error, state `Failed`  
- [ ] Works after Reachy user said yes to “should I bring it?”

## Do not block on

Opening packaging, pill counting, multi-room chores, or replacing the conversation TTS stack.
