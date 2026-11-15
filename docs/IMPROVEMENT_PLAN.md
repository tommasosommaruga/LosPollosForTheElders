# Medicine Buddy: audit and improvement plan

Written after an audit of v0.4.x (Reachy Mini app, PR #1). Findings marked **[verified]** were
reproduced by running code; **[inferred]** come from logs or reading code; **[to check]** need the robot.

## 0. What works (verified)

- **Internet search** (`safe_web_search`) returns real, correctly encoded results (weather, health facts) in
  3-6 s, strips links, cleans instruction-like sentences, and fails gracefully (0.9 s, generic error, no stack
  trace) when the search service is unreachable. The robot only gets this tool, not Pollen's raw search tools.
- Schedules by voice, per-person passphrases, voice guard, family memories, in-app pages and debug view:
  188 tests pass.

## 1. P0: bugs that break the core flow (FIXED in this branch, 230 tests)

Status: all five are fixed. #1 proactive reminders (`reminders.py`, `speaker.py`), #2 manual taken/missed (`orchestrator.finish_adherence`), #3 crash-proof console output (`dry_run.py`), #4 demo-mode announcement (`api.run_dose`), #5 time zone (`clock.py`, Family page). Still to verify on the robot: that reminders are actually spoken (needs the app updated there).

| # | Problem | Evidence | Fix | Test |
|---|---|---|---|---|
| 1 | **Reachy never reminds on its own.** Nothing starts a conversation when a dose is due; `find_due_doses` only runs when someone asks (or the page loads). For a *reminder* app this is the biggest gap. | **[verified]** code search: no timer/loop anywhere | Background `ReminderLoop` in `main.py` (every 30 s): when a dose is due, not yet reminded, and the robot is idle, make Reachy say it (reuse the conversation app's greeting/`conversation.say` path). Repeat after 10 min, mark Missed after 60 min, then alert the caregiver (see 12). | Fake clock + fake speaker: due -> one reminder -> repeat -> missed |
| 2 | **"I took my aspirin" fails** unless the arm dispensed first: `Adherence requires state Placed, found None`. With no arm connected this is the normal case. | **[verified]** probe | Allow Taken/Missed from Due/Reminded/no open dose (manual confirmation); create the Due event on the fly; keep the Placed path for the arm. | `mark_dose_taken` with no prior dispense -> Taken |
| 3 | **Simulated dispense crashes on non-UTF-8 consoles** (`print("... -> bowl")` with an arrow -> `charmap` error) and Reachy then says *"The arm failed while moving. Please check the e-stop."* Happens on the Windows laptop when output is logged. | **[verified]** probe | Use ASCII/`logging` instead of `print` in `dry_run.py`; don't blame the e-stop for non-hardware exceptions. | Run dispense with `PYTHONIOENCODING=cp1252` |
| 4 | **Dry-run says the medicine "is in the bowl"** although nothing moved. Misleading to a real user. | **[verified]** `AppConfig(dry_run=True)` hard-coded in `main.py` | When `dry_run`, speak "demo mode: I can't bring it yet, please take it from your box"; show "DEMO" banner in the pages. | speak_hint contains the demo notice |
| 5 | **Time zone.** The robot's clock is UTC; you are UTC+1, so a 9 AM reminder would fire at 10 AM local. | **[inferred]** robot log timestamps `+00:00` vs local wall clock; scheduler uses the machine's clock | Time zone setting (default from the browser/Family page), stored in the DB, used by scheduler and speech via `zoneinfo` (+ `tzdata` on Windows). Show "robot time" on the debug page. | Scheduler tests across zones and DST |

## 2. P1: internet search, privacy, security

| # | Problem | Evidence | Fix |
|---|---|---|---|
| 6 | **Personal data can leave in a search.** The query scrubber only removes links/emails/long numbers. `"Mario takes metformin at 9 AM, is it dangerous"` and `"my passphrase is old fishing boat"` both pass through to the third-party search service. | **[verified]** | Route searches through the service (it knows names and passphrases): refuse queries containing a registered name, a passphrase, or a name + medicine combination; allow generic questions ("what is vitamin D"). Show blocked searches on the debug page. |
| 7 | Search is silent for 3-6 s, then Reachy talks. No cache, no retry. | **[verified]** timings | Spoken "let me look that up", 8 s timeout, one retry, 10-minute cache, "nothing relevant found" handling (a nonsense query returned an unrelated page). |
| 8 | Single external dependency (a public Hugging Face Space) with unknown rate limits and uptime. | **[inferred]** | Pluggable provider (env var): Space (default), self-hosted SearXNG, or an API-key provider; status of the provider on the debug page. Searches are currently not visible in the debug request log (they bypass the service). |
| 9 | **Passphrases appear in Pollen's console log** (transcripts and tool arguments). | **[verified]** earlier session | Logging filter in `main.py` that redacts `passphrase*` arguments and any transcript containing a known passphrase (`secret_in_text`). |
| 10 | **First-run takeover:** while nobody is registered, anyone on the Wi-Fi can open `:7860/medicine` and register themselves as caregiver. | **[verified]** `test_open_while_nobody_registered` | Setup code: Reachy says/prints a one-time 4-digit code at first start; creating the first caregiver needs it (or only allow loopback in setup mode). |
| 11 | **Voice and medical data go to a cloud backend** (Hugging Face realtime). Families should know. | **[inferred]** | Consent text on first run; document the local/self-hosted backend option (`HF_REALTIME_CONNECTION_MODE=local`); never log audio. |

## 3. P2: product features and robustness

| # | Item | Notes |
|---|---|---|
| 12 | **Caregiver alerts**: missed dose / repeated refusal -> push notification (Telegram/ntfy/email) | **Done (v0.7):** ntfy/webhook via `MEDICINE_BUDDY_ALERT_URL` + Family page test button. SMS/email still later. |
| 13 | **Multilingual**: family speaks English, Italian and French (seen in the logs); catalog aliases, weekday words ("lunedi", "lundi") and yes/no detection are English only | Localised alias lists; language per person. |
| 14 | **Physical confirmation** for risky actions (dispense, delete): antenna touch/press instead of "anyone in the room can say yes" | Pollen's "antenna as button" pattern. Voice identification is a research topic, not a quick fix. |
| 15 | Catalog is fixed at 10 medicines | Caregiver adds/edits medicines on the Family page (still no free text for the voice). |
| 16 | **Legacy features**: keep the original voice recording of a story, export a family book (PDF), search by topic/person, share with relatives remotely | The memory store holds text only today. |
| 17 | **Backups**: nightly DB copy + export (JSON/CSV) from the Family page; retention for dose events | On the robot the DB lives in `~/.local/share/medicine-buddy`. |
| 18 | `_Sessions` has no lock; login limits are per IP (all devices behind one NAT share a limit) | Small hardening. |

## 4. P2: engineering

| # | Item |
|---|---|
| 19 | **Check the robot install doesn't downgrade Pollen's conversation app.** `pip install` pins `reachy_mini_conversation_app` to git tag v1.2.0, which may replace the version preinstalled in the robot's `apps_venv`. **[to check]** on the robot (`pip list`); consider a version range instead of a git pin. |
| 20 | **CI**: GitHub Actions running `pytest` (Python 3.11 and 3.12) and `reachy-mini-app-assistant check` on every PR; branch protection on `main`. |
| 21 | The tool wrappers (`conversation/external_tools/*.py`) are not unit-tested: add contract tests that load each tool with a stub of the conversation app and check its schema against the API. |
| 22 | Full voice walkthrough on the robot (first-time passphrase, add/move/remove reminder, memory save/recall) recorded as a manual test script in `DEMO_CHECKLIST.md`. |

## 5. P3: hardware (other team)

Real `VisionPort`/`ArmPort`, tray calibration, place-verify, e-stop. Contract and handoff are in `docs/ARM_TEAM_HANDOFF.md`.

## Suggested order

1. P0 #2, #3, #4 (small, remove visible failures) -> then #5 time zone -> then #1 proactive reminders.
2. P1 #6 and #9 (privacy leaks), then #10.
3. #19 and #20 before the next robot install, so updates are safe and checked automatically.
4. Everything else by what the family tries first.
