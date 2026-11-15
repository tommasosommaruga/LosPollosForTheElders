# Medicine Buddy — current status (single check)

**Last updated:** 2026-11-15 · **Code version in this checkout:** `0.9.1` (`medicine_buddy.__version__`)  
**Last git commit on `main`:** `65fa163` — *Fix muted Reachy speech after patient-listen VAD patch.*

Use this file to see **what is plugged in and working**, **what is partial / broken**, and **how the pipeline runs**, for **both** places the app lives.

---

## 1. Two places the app runs

| | **A — This laptop / git checkout** | **B — Reachy Mini (Wireless robot)** |
|--|--|--|
| **How it runs** | `./scripts/run_robot_host.sh` or `python -m medicine_buddy.main --host <robot-ip>` | Reachy Mini Control → Applications → **Medicine Buddy** (daemon starts `python -u -m medicine_buddy.main`) |
| **Install** | `./scripts/bootstrap.sh` → editable `pip install -e ".[dev]"` | `./scripts/install_on_robot.sh [host] [ref]` → `pip install` from **GitHub `main`** into `/venvs/apps_venv` |
| **Code source** | Files on disk (may include **uncommitted** WIP) | Whatever git ref was last installed (default **`main`**) |
| **DB path** | `apps/medicine_buddy/data/medicine_buddy.db` (checkout) | `~/.local/share/medicine-buddy/medicine_buddy.db` |
| **UI** | `http://127.0.0.1:7860/medicine` (+ API `:8787`) | `http://<robot>:7860/medicine` (API on robot loopback `:8787`) |
| **Hardware Reachy** | Needs `--host` / `REACHY_HOST` + often `REACHY_MINI_MEDIA_BACKEND=no_media` on Mac | On-robot: full media / speech stack |
| **Maker Arm** | **Not wired** — always `AppConfig(dry_run=True)` in `main.py` today | Same — **simulated arm** until hardware ports are implemented |

**How to check which version is where**

1. Open **Debug** on each machine (`/medicine` → Debug): read `app.versions.medicine_buddy`.
2. Robot only has what was last pip-installed from git. **Uncommitted laptop files are NOT on the robot** until you commit, push, and re-run `install_on_robot.sh`.
3. As of this STATUS write-up: laptop has **extra uncommitted** companion/alert modules (see §5); robot on `main@65fa163` does **not**.

---

## 2. Locked decisions (plugged in by design)

| Decision | Status | Where |
|----------|--------|--------|
| Reachy Mini = voice companion (Pollen conversation app) | **Working** | `main.py` locks profile `medicine_caregiver` |
| Maker Arm = pick labeled **closed** box → bowl | **Contract only** | `ports.py`; live path still Fake* |
| Arm `move_object` for bottles / other graspables | **Contract** (`0.9.1`) | POST `/v1/move_object` JSON; arm team owns motion |
| Classical OCR + taught waypoints (not VLA) | **PART** | Buddy `read_document` tool (Tesseract) for papers in front of Reachy; tray OCR + arm waypoints still stubs |
| Confirm-before-move every time | **Working** | `run_dose` + voice guard + tool |
| No dosing advice / no open packaging / no lift people | **Working (policy)** | profile + AGENTS.md |
| Dry-run default until arm team wires hardware | **Forced on** | `main.py` → `AppConfig(dry_run=True)` |
| Swiss oral medicine catalog (~1490) | **Working** | `medicine_catalog.json` |
| Per-person passphrases + caregiver override | **Working** | `access.py`, Family page |
| Master switch: passphrases on/off | **Working** | Family page / `MEDICINE_BUDDY_REQUIRE_PASSPHRASES` |
| Time zone for reminders | **Working** | Family page / `MEDICINE_BUDDY_TZ` |
| Proactive spoken reminders when idle | **Working** | `reminders.py` + `RpcSpeaker` |
| “I took it” without arm dispense | **Working** | `orchestrator.finish_adherence` |
| Family memories (consent before save) | **Working** | tools + DB |
| Safe web search (scrubbed, untrusted) | **Working** | `safe_web_search` + `web_safety.py` |
| In-app UI: dashboard, history, conversation, DB, family, debug | **Working** | `/medicine/*` |
| Companion calendar (ICS subscribe + import proposals) | **Working (laptop WIP until ship)** | Family page + voice tools; meds still SQLite |
| Voice → Google Calendar write (`0.10.0`) | **Working when OAuth env set** | `create_calendar_event` / cancel push via Calendar API; Family calendar id + Test |
| Unified clock + calendar timeline (`0.9.0`) | **Working (laptop WIP until ship)** | `get_robot_time`; `list_upcoming_events(when=…)`; med mirror; harder ICS import; Family unified list |
| Install path: git → robot apps_venv | **Working** | `scripts/install_on_robot.sh` |

---

## 3. Pipeline — how a dose runs *now*

```text
Schedule in SQLite ──► due window (tz-aware)
        │
        ▼
ReminderLoop (30s) ── if idle ──► Reachy speaks “time for X — bring it?”
        │                              (repeat 10 min ×3; Missed after 60 min)
        ▼
Person says yes ──► voice guard ──► dispense_medicine → POST /doses/run
        │
        ▼
DoseStateMachine (orchestrator)
  Due → Confirmed → Locating → Picking → Placed → Watching → Taken|Missed|Failed
        │
        ├─ TODAY: FakeVision + FakeArm + FakeAdherence  (dry_run)
        │         speak_hint: “demo mode… take it from your own box”
        │
        └─ TARGET: HardwareVisionOcr + HardwareArmController + camera adherence
                   (stubs raise NotImplementedError — not selected by main.py yet)
```

| Stage | Working? | Notes |
|-------|----------|--------|
| Voice schedule add/update/delete | **Yes** | Catalog-gated; passphrase optional via master switch |
| Due detection | **Yes** | 15 min before → 60 min after |
| Proactive reminder speech | **Yes** | Idle-safe; needs conversation session |
| Confirm gate | **Yes** | |
| OCR locate box (tray / arm) | **Simulated only** | `FakeVision`; `HardwareVisionOcr` stub |
| OCR read paper in front of Buddy | **PART** | `read_document` phases: **guide → check (position feedback) → hold still → capture + OCR** (`document_ocr.py`; needs `.[ocr]` + Tesseract) |
| Arm slot→bowl | **Simulated only** | `FakeArm`; `HardwareArmController` loads JSON then `NotImplementedError` |
| Place verify | **Simulated only** | |
| Adherence camera | **No** | `adherence.py` stub; voice / manual Taken|Missed **works** |
| Persist states | **Yes** | `dose_events` in SQLite |
| Missed → caregiver push | **Laptop WIP only** | See §5; not on robot `main` yet |

**Fetch / rehab / general house-help arm motions:** documented in `house-help-ideas.md` — **not implemented** (ports are meds-only: `replay_slot_to_bowl`).

---

## 4. Feature matrix (both deployments)

Legend: **OK** = works end-to-end · **PART** = code present but incomplete/demo · **NO** = missing · **WIP** = in this checkout, not on robot `main`

| Feature | Laptop checkout | Robot (last `main` install) |
|---------|-----------------|------------------------------|
| Conversation + medicine tools | OK | OK |
| Schedule / list / due / mark taken | OK | OK |
| Dispense confirm → state machine | OK (fake arm) | OK (fake arm) |
| Real Maker Arm motion | NO | NO |
| Real dual-cam OCR | NO | NO |
| Proactive reminders | OK | OK (verify tz on Family page) |
| Wake greeting | OK | OK |
| Passphrases + voice guard | OK | OK |
| Family memories (stock speech; no VAD stretch) | OK | reinstall for 0.8.1 |
| Safe web search | OK | OK |
| History / adherence reports | OK | OK |
| Live conversation transcript UI | OK | OK |
| Time zone setting | OK | OK (must set once — robot often UTC) |
| DEMO MODE banner / honest dry-run speech | OK | OK |
| Caregiver Missed alerts (ntfy/webhook) | WIP | NO (until push + reinstall) |
| Companion check-in loop | WIP | NO |
| Family video-call (live) | NO — mock “coming soon” only | NO |
| `suggest_activity` voice tool | WIP | NO |
| Companion calendar ICS + voice events | WIP (`0.8.0`) | NO (until push + reinstall) |
| Unified clock / `when` timeline (`0.9.0`) | WIP | NO (until push + reinstall) |
| Smart-home / scam-call / games | NO | NO |
| Multi-room / multi-arm chores | NO | NO |

---

## 5. This checkout vs robot — drift (important)

**On laptop now (uncommitted / not on `main` until you ship it):**

- `caregiver_alerts.py`, hook from `reminders.py` on Missed  
- `checkins.py` + start from `main.py`  
- `companion_settings.py`, `activities.py`  
- Family page panels (alerts, companion toggles; video call = coming-soon mock)  
- Tools: `start_family_call.py` (coming-soon stub), `suggest_activity.py` (+ profile entries)  
- Docs touch: README / plan / house-help / IMPROVEMENT_PLAN / `.env.example`  

**Intentionally reverted / corrected already:** dispense must ask **“Should I bring it?”** for the **arm** — not “take it yourself first.”

**Robot stays on last installed git ref** until:

```bash
./scripts/install_on_robot.sh reachy-mini.local main   # or a branch SHA
```

---

## 6. Partial / broken / to fix (priority for the product)

### P0 — core demo with hardware (Track 4 arm)

| Item | State |
|------|--------|
| Wire `VisionPort` / `ArmPort` for real cameras + Maker Arm | **Stub only** — `vision_ocr.py`, `arm_controller.py` |
| `main.py` / `api.py` always construct Fake* | **Must switch** when `dry_run=false` |
| Teach/load waypoints under `data/waypoints/` | Loader exists; **replay not wired** to maker-arm-sdk |
| Camera indices in `agents.local.md` | Config present; **unused by hardware code** |
| E-stop / first live move confirm | Policy in docs; **not coded** into arm driver |
| Reachy camera adherence | **NotImplemented** |

### P1 — reliability on the robot

| Item | State |
|------|--------|
| Time zone wrong if Family page never set | PART — easy miss on fresh robot |
| Reminder speech depends on idle RPC | OK in tests; **re-verify on robot after each install** |
| `pip install` may pin conversation app to git v1.2.0 | Watch for downgrade of robot’s preinstalled app ([IMPROVEMENT_PLAN](docs/IMPROVEMENT_PLAN.md) #19) |
| Voice / medical audio via cloud HF backend | Working but privacy trade-off (#11) |
| Passphrases can appear in Pollen console logs | Known limit |

### P2 — product extras (not arm)

| Item | State |
|------|--------|
| Caregiver remote alert on Missed | WIP locally |
| Companion check-ins / activity prompts / call URL | WIP locally — **not** the Maker Arm goal |
| Multilingual schedule words | English-centric |
| Catalog edit on Family page | Fixed catalog file |
| Embedded video / SMS | NO |

---

## 7. Voice tools (profile `medicine_caregiver`)

**On robot `main` (and base set):**  
`register_person`, `list_people`, `save_memory`, `recall_person_memories`, `forget_memory`, `schedule_medicine`, `update_medicine_schedule`, `delete_medicine_schedule`, `list_medicines`, `check_due_medicines`, `dispense_medicine`, `move_object`, `mark_dose_taken`, emotions/head/camera/volume, `safe_web_search` (+ calendar / companion tools in newer installs).

**Also in this laptop checkout (WIP):**  
`start_family_call`, `suggest_activity`, `get_robot_time`, `list_upcoming_events` (with `when`), calendar create/cancel.

---

## 8. Quick health check (same steps both places)

1. App running → open `/medicine` → sign in → Debug: version, `dry_run: true`, reminders running.  
2. Family page → set **time zone**.  
3. Voice: add a test medicine → wait or force due → reminder speaks when idle.  
4. “Yes, bring it” → hear **demo mode** (arm simulated).  
5. “I took it” → dose **Taken** on dashboard / History.  
6. **Arm live:** only after hardware ports exist and dry_run is off — e-stop ready.

CLI smoke (laptop):

```bash
cd apps/medicine_buddy && source .venv/bin/activate
pytest -q
medicine-buddy demo --dry-run
```

---

## 9. One-line verdict

**Working now (laptop + robot):** Reachy voice companion + schedule DB + confirm + **simulated** dispense + adherence-by-voice + family UI/memories/reminders.  
**Not working yet (either place):** real **Maker Arm** OCR/motion/place-verify — the Track 4 hardware spine.  
**Laptop-only WIP:** caregiver alerts / check-ins / activity suggestions — not installed on the robot until shipped to git and reinstalled. Family video call is mock “coming soon” only.
