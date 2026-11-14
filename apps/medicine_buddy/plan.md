# Medicine Buddy — plan

## Intent

First version: dry-run end-to-end dose pipeline with SQLite persistence, confirm gate, OCR confidence gate, place verify, adherence voice fallback. Hardware Reachy/Maker Arm wired behind ports later.

## Approach

- Explicit `DoseStateMachine` + port interfaces
- Console buddy for dry-run yes/no
- Fake tray map for OCR
- Logged waypoint replay (no live motors yet)

## Open questions (answer when ready)

- [ ] Overhead / wrist camera device indices on demo laptop?
- [ ] How many tray slots in the physical demo?
- [ ] Prefer antenna button confirm in addition to voice?
