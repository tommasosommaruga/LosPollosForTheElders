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

## v0.7.0 — demo passphrase switch + patient story listening (done)

- Master `require_passphrases` setting (Family page / env, default off): one switch disables all spoken-passphrase + voice-guard checks while keeping the code
- `listen_patiently` tool + live ServerVad silence stretch (~3.5s) only during story/memory dictation; restores normal ~0.7s after stop / save_memory / timeout
- Profile: freer emotions (`play_emotion`, `dance`, `move_head`, `sweep_look`, …); package version bumped (health uses `__version__`)
- Tests: `test_listen_mode.py`, passphrase master-switch coverage in `test_family_settings.py`

## v0.9.0 — unified clock + calendar timeline (done)

- `get_robot_time` voice tool (Family timezone); profile: never invent date/time or events
- `list_upcoming_events(when=…)` unified meds + companion window (`soon` = now → end of tomorrow)
- Schedule “tomorrow” / relative weekdays; catalog clear-winner + adrenaline aliases
- Medicine rows mirrored into `calendar_events`; harder ICS import sync; Family unified list + Google reachability hint

## v0.8.12 — family video call → coming soon mock (done)

- Removed working call-link save / Open call / URL bridge behaviour
- Family page shows disabled mock panel; voice `start_family_call` says use phone/tablet

## v0.8.8 — back button only (done)

- Restored **← Medicine Buddy** on Pollen `/` and sticky **← Dashboard** nav (no wake-robot)

## v0.8.7 — rollback crashy features, keep calendar TZ (done)

- Reverted to pre-`0ab191f` (dropped nav + wake-robot); kept ICS UTC/TZID fix

## v0.9.2 — arm listener + speak/log POST (done)

- `integrations/arm/arm_listener.py` for arm PC (hooks: `run_dispense` / `run_check_taken` / `run_move_object`)
- Buddy logs full JSON POST/response; Reachy `speak_hint` narrates sending the arm package

## v0.9.1 — generic arm move_object (done)

- Voice tool `move_object` (confirm-gated) POSTs `medicine_buddy.arm.v1` `/v1/move_object` for bottles and other graspables
- Medicines still use `/v1/dispense`; empty ARM_URL → demo speak_hint; arm team owns vision/motion
- Handoff: `docs/ARM_TEAM_HANDOFF.md`; tests in `test_arm_protocol.py`

## v0.8.3 — arm HTTP JSON protocol (done)

- After voice confirm, Buddy POSTs structured `medicine_buddy.arm.v1` JSON to `MEDICINE_BUDDY_ARM_URL`
- `/v1/dispense` (one-shot locate/pick/place) + `/v1/check_taken` (one-shot camera)
- Empty ARM_URL → FakeArm demo; handoff doc updated

## v0.8.2 — open UI, no caregiver-only gates (done)

- `/medicine` Dashboard, Database, History, Family, Debug open by default (no caregiver login)
- Optional `MEDICINE_BUDDY_REQUIRE_UI_LOGIN=1` — any registered person's passphrase works
- Voice: when passphrases ON, any family passphrase authorises changes (caregiver role is a label)

## v0.8.1 — stock Reachy speech (done)

- Removed patient-story VAD / `listen_patiently` turn-stretching; conversation uses normal Reachy Mini speech-to-speech again
- Memories still work (`save_memory`); profile no longer asks for long-silence listening

## v0.8.0 — companion calendar (ICS) (done)

- Bidirectional ICS: Family page subscribe URL (Outlook/Google/Apple) + paste import ICS URL
- Medicine schedules stay source of truth in SQLite; export as `Med: …` events
- Import: companion events auto-upsert; medicine-like items become **proposals** (Accept/Reject) before writing schedules
- Voice tools: `list_upcoming_events`, `create_calendar_event`, `cancel_calendar_event`
- Background sync loop (`MEDICINE_BUDDY_CALENDAR_SYNC`); works on laptop + Reachy Mini `/medicine`
- Tests: `test_calendar.py`

## v0.7 — elderly-care pillars (done)

Maps geriatric needs onto the app, most effective first:

1. **Caregiver alerts** — Missed doses POST to `MEDICINE_BUDDY_ALERT_URL` / Family page URL (ntfy or webhook); test button; activity log
2. **Adaptive assist** — `encourage_self_care` (default on): dispense hints + profile prefer self-serve before arm
3. **Companion check-ins** — idle `CheckInLoop` with quiet hours / interval; logged as activity
4. **Family video bridge** — coming-soon mock on Family page; `start_family_call` tells them to use phone/tablet (no live URL bridge yet)
5. **Activity prompts** — `suggest_activity` catalog (motor / cognitive / social) + optional nudges in check-ins
6. Docs: README north star / roadmap, this plan, house-help build order

Tests: `test_caregiver_alerts.py`, `test_companion_pillars.py`

## v0.6.2 — easier first-time passphrase + medicine setup (done)

- `schedule_medicine` can enrol a new person with `new_passphrase` after they say it twice, then save the reminder in one flow
- Clearer `needs_passphrase_setup` / `needs_passphrase_repeat` with steps + speak_hint; caregiver `passphrase` never misused as the new secret
- Saying the passphrase twice in one utterance counts; profile/tool copy updated

## v0.6.1 — wake greeting (done)

- On app start, once Reachy can speak: a short cool wake line, then which medicines are due in the next 30 minutes (from real schedules). Retries until the conversation session is ready; off via `MEDICINE_BUDDY_WAKE_GREETING=0`.
- Tests: `test_wake.py`

## v0.6 — activity + history explorer (done)

- Persist privacy-safe `activity_events` in SQLite (dual-write from `record_request`; RAM deque kept for debug snapshot; prune by age/row cap)
- Auth-gated `/medicine/api/*` on the in-app UI (session cookie): activity cursor, cross-entity search, person records, record detail, adherence reports, agent status
- History / People / Record pages inside Reachy Mini Control (`/medicine/history`, `/medicine/people/{id}`, `/medicine/records/{kind}/{id}`)
- Exact person matching only in the explorer (no Maria→Mario); never expose passphrase hashes or utterance text
- Stories = `memories` rows; dose timelines = `dose_events` rows (no separate history table)
- Tests: `test_explorer_db.py`, `test_explorer_api.py` (+ extended web UI coverage)

## Open questions (answer when ready)

- [x] Which medicines belong in the catalog? Now the Swiss oral-medicine list (~1,490 entries: ingredient names + brand aliases), generated by `scripts/import_swissmedic.py` from Swissmedic's extended list (31.10.2026). The original 10-entry demo catalog is kept as `medicine_buddy/medicine_catalog.demo.json` (use via `MEDICINE_BUDDY_CATALOG`). Tray still holds Aspirin + Vitamin D.

- [ ] Overhead / wrist camera device indices on demo laptop?
- [ ] How many tray slots in the physical demo?
- [ ] Prefer antenna button confirm in addition to voice?
