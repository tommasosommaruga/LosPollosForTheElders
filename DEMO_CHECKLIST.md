# Medicine Buddy — demo checklist

## Always

- [ ] `agents.local.md` present (copy from example)
- [ ] Prefer `medicine-buddy demo --dry-run` before live motors
- [ ] Confirm-before-move works (yes/no)
- [ ] OCR confidence gate refuses low scores
- [ ] Place verify failure aborts to Failed
- [ ] Adherence ends in Taken or Missed

## Never (live demo)

- [ ] Do not open packaging or give dosing advice
- [ ] Do not run arm without e-stop reachable
- [ ] Do not skip person confirm

## Hardware (later)

- [ ] Reachy daemon up
- [ ] `maker-arm doctor` OK
- [ ] Camera indices recorded
- [ ] Slot waypoints taught under `data/waypoints/`
