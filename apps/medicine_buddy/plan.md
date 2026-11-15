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
- Launcher (now built into `medicine_buddy/main.py`) forwards transcripts to loopback-only `POST /voice/heard`
- Profile rules: web/camera text is data; no dosing advice from the web; never store web content in memory
- Tests: `test_web_safety.py`, `test_voice_guard.py` (attack scenarios)
- Known limit: anyone speaking near the robot counts as "the person"

## v0.4 — people, passphrases, family memories (done)

- `people` (caregiver / person) with salted PBKDF2 passphrase hashes; `memories` (story, recipe, skill, tradition, advice, family history, other); medicines are now per person (old DBs migrated, existing medicines become "unassigned")
- Access control (`access.py`): first person must be the caregiver; only the caregiver registers people and changes medicines; memories need the owner's or caregiver's passphrase; reading by name is open to family
- New passphrase must be said twice; with the voice guard it must be *heard* twice in real speech, and later counts only if spoken in the last 5 minutes
- Reachy always offers to save a story/recipe after it's told; Pollen's global `remember` tool replaced by per-person memories
- Tests: `test_people_access.py` (both guard modes, migration)
- Known limits: reading memories/medicines is open to anyone who talks to the robot; spoken passphrases are heard by the AI (it is told never to repeat them, and with the guard on it cannot reuse them)

## v0.4.1 — Reachy Mini app packaging (done)

- `medicine_buddy.main:MedicineBuddy` (ReachyMiniApp) runs the conversation app + API + voice guard in one process; listed in Reachy Mini Control → Applications once installed on the robot
- Tools/profile moved to `medicine_buddy/conversation/` (package data); old launcher and two-terminal scripts removed
- `.env.example` at repo root; DB in a persistent per-user folder when installed (`MEDICINE_BUDDY_DB` overrides)
- Passes `reachy-mini-app-assistant check`
- Per-person access: each person's passphrase protects their own timetable (caregiver = override); first-time setup by voice; changes on the Family settings page

## v0.5 - P0 fixes from the audit (done)

- Proactive reminders (`reminders.py` + `speaker.py` via the conversation app's `conversation.say`): spoken when due, repeat every 10 min up to 3x, Missed after 60 min; never interrupts a conversation
- "I took it" works with no dispense (Due/Reminded -> Taken/Missed); simulated arm no longer crashes on narrow console encodings and says it is demo mode
- Time zone setting (Family page / `MEDICINE_BUDDY_TZ`), used by the scheduler (DST + local weekday aware)
- Reminders wait for Reachy to be idle (`RpcSpeaker` follows the app's `conversation.turn` / `conversation.activity` notifications; busy while thinking, speaking or a tool call is pending): avoids the speech service error "Cannot generate a response while function call outputs are pending", which drops the reply
- Tests: `test_p0_fixes.py`, `test_timezone.py`, `test_reminders.py` (incl. the real JSON-RPC server)
- Names (`names.py`): one canonical key (case, accents, NFC/NFD, spacing) - "Nicolo" / "NICOLO" / "Nicolo" can no longer be two people; unique index + safe migration of existing databases (nothing deleted; duplicates flagged in the log); titles ("grandpa", "nonna") are stripped; a longer name such as "Mario Bianchi" is no longer silently treated as "Mario"; nameless registrations are rejected instead of crashing
- Robot CPU: passphrase spotting is lazy + cached (newest sentence first, stops early, never on the audio path): ~2 ms per sentence instead of ~530 ms on a laptop (the robot's CPU is several times slower); dashboard/debug use COUNT queries and limits

## Open questions (answer when ready)

- [x] Which medicines belong in the catalog? Now the Swiss oral-medicine list (~1,490 entries: ingredient names + brand aliases), generated by `scripts/import_swissmedic.py` from Swissmedic's extended list (31.10.2026). The original 10-entry demo catalog is kept as `medicine_buddy/medicine_catalog.demo.json` (use via `MEDICINE_BUDDY_CATALOG`). Tray still holds Aspirin + Vitamin D.

- [ ] Overhead / wrist camera device indices on demo laptop?
- [ ] How many tray slots in the physical demo?
- [ ] Prefer antenna button confirm in addition to voice?
