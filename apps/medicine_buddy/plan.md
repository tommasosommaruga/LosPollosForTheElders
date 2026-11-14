# Medicine Buddy — plan

## Intent

First version: dry-run end-to-end dose pipeline with SQLite persistence, confirm gate, OCR confidence gate, place verify, adherence voice fallback. Hardware Reachy/Maker Arm wired behind ports later.

## Approach

- Explicit `DoseStateMachine` + port interfaces
- Console buddy for dry-run yes/no
- Fake tray map for OCR
- Logged waypoint replay (no live motors yet)

## v0.2 — voice schedule management (done)

- Medicine names restricted to `medicine_catalog.json` (aliases + fuzzy match for STT errors); unknown names rejected with suggestions
- Voice update / delete of schedules (`update_medicine_schedule`, `delete_medicine_schedule`); delete asks for confirmation; soft delete keeps history
- Every add/update is read back from SQLite and confirmed aloud ("Aspirin is set up correctly: every day at 9 AM")
- Status page at `GET /`
- Fixes: due window instead of exact minute; duplicate schedules; "9 AM"/"weekdays" parsing; DB lock for threaded server; CLI DB path independent of CWD; "*" no longer spoken; malformed requests get a 400 instead of a dropped connection
- Tests: `tests/test_db.py` (database), `test_catalog.py`, `test_schedule_voice_api.py` (incl. real HTTP server + page)

## v0.3 — internet access + prompt-injection protection (done)

- `safe_web_search` tool: Pollen search Space, snippets + site name only, injection-like sentences / hidden unicode / markup / links stripped, query scrubbed of links & personal data (`web_safety.py`)
- VoiceGuard (`serve --voice-guard`): medicine actions need the person's transcribed words — medicine named in the last 3 min; delete/dispense need a "yes" after the question (single use, 2 min expiry); unasked `confirmed=true` turns into a question
- Cross-platform launcher `integrations/reachy_conversation/run_conversation.py` forwards transcripts to loopback-only `POST /voice/heard`
- Profile rules: web/camera text is data; no dosing advice from the web; never store web content in memory
- Tests: `test_web_safety.py`, `test_voice_guard.py` (attack scenarios)
- Known limit: anyone speaking near the robot counts as "the person"

## Open questions (answer when ready)

- [ ] Which medicines belong in the catalog for the demo? (currently 10 common ones; tray has Aspirin + Vitamin D)

- [ ] Overhead / wrist camera device indices on demo laptop?
- [ ] How many tray slots in the physical demo?
- [ ] Prefer antenna button confirm in addition to voice?
