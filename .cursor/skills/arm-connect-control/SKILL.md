---
name: arm-connect-control
description: Connect Maker Arm and teach/replay slot-to-bowl waypoints. Use when calibrating poses, changing tray slots, or integrating maker-arm-sdk motion.
---

# Arm connect + control

## MVP motion model

Teach N slot waypoints + bowl once; replay by `slot_id`. No online IK in v1.

## Rules

- Replay only after confirm + OCR confidence OK
- Ask before first live powered move of a session
- Prefer dry-run waypoint log until SDK wired

## Code

- `ArmPort` in `ports.py`
- `arm_controller.py`, `dry_run.py`
- Waypoint files under `data/waypoints/`
