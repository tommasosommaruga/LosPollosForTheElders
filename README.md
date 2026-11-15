# Los Pollos For The Elders — Medicine Buddy

**Hackathon Track 4: Elderly Care & Rehabilitation** (daily help, rehab, mobility)

A home assist system that pairs:

| Role | Hardware | Job in this project |
|------|----------|---------------------|
| **Buddy** | [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) | Talk, remind, confirm, watch adherence (TTS/STT via pollen conversation app) |
| **Arm** | [MakerMods Maker Arm](https://www.makermods.ai/maker-arm) | Pick labeled medicine boxes → place in a **bowl** (cameras + motion; team-owned) |

**Team split (current):** this repo owns scheduling, safety state machine, Reachy conversation tools, and HTTP orchestration. **Another subgroup owns Maker Arm software, dual cameras, and movement training.** Integration is through clear ports/API contracts below so both sides can ship in parallel.

Related notes: [`context.md`](context.md) · [`house-help-ideas.md`](house-help-ideas.md) · agent ops: [`AGENTS.md`](AGENTS.md)

---

## What we want to reach

### North star (product vision)

Help an older person live safer and more independently at home, aligned with three geriatric pillars:

1. **Chronic care continuity (medicines)** — know *what* to take and *when*; remind gently; bring the correct closed box only when needed; check they took it; **alert caregivers on Missed**.
2. **Motor autonomy** — encourage self-care and light movement first; arm assist for fetch/bring when the person cannot safely reach (see [`house-help-ideas.md`](house-help-ideas.md); arm team owns hardware motion).
3. **Cognitive / emotional + caregiver support** — companion chat, consented family memories, wellbeing check-ins, optional activity prompts, and a **family video-call link** for remote relatives.

**Adaptive rule:** assist only where the person cannot manage physically or cognitively; otherwise stimulate activity and conversation so independence does not atrophy.

We are **not** a doctor. No dosing advice, no opening packaging in the MVP, no lifting people.

### Demo MVP (what this codebase targets now)

End-to-end **medicine reminder + dispense + adherence**:

1. Person (by voice via Reachy) stores usual pills and recurrence → **SQLite schedule**.
2. At due time (or force-trigger), Reachy reminds them and (when encourage-self-care is on) prefers that they take the box themselves; offers the arm only if they ask or cannot reach it.
3. On clear **yes** to bringing it, dual arm cameras + OCR find the **standardized labeled box** (empty slots allowed).
4. Arm replays taught motion: **slot → bowl** (bowl preferred over plate for detection).
5. Arm cams **place-verify**; Reachy cam / voice confirms **taken** or logs **missed** (Missed can push a caregiver alert).

**Locked technical choices**

- Classical control first: OCR + confidence gate + **taught waypoints** (not Dream-1/VLA for MVP).
- Confirm-before-move every time.
- Dry-run fakes until arm/vision hardware ports are wired by the arm team.
- Reachy TTS/STT stays on pollen’s **`reachy_mini_conversation_app`** (do not reinvent voice).

### Physical setup we assume

- Fixed tray of standardized boxes; **unique large text** per medicine; unused slots empty / `EMPTY`.
- Fixed **bowl** drop pose in the workspace.
- Maker Arm: wrist camera + overhead camera (both included with the kit).
- Reachy Mini beside the arm, facing the person.
- Optional: leader/training arm for demos; actuated arm for live assist ([Maker Arm](https://www.makermods.ai/maker-arm)).

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│ Robot host PC (Reachy connected; git pull this repo)        │
│  ┌──────────────────────────┐  ┌─────────────────────────┐  │
│  │ reachy_mini_conversation │  │ medicine-buddy serve    │  │
│  │ (TTS / STT / LLM)        │─▶│ :8787  HTTP API         │  │
│  │ + external medicine tools│  │ schedule · state machine│  │
│  └──────────────────────────┘  │ VisionPort · ArmPort    │  │
│                                 └───────────┬─────────────┘  │
└─────────────────────────────────────────────┼────────────────┘
                                              │
                    arm team implements real  │  Fake* today
                    VisionPort + ArmPort ─────┘
                                              ▼
                                    Maker Arm + 2 cameras
```

### Dose state machine (persisted)

`Idle → Due → Reminded → Confirmed → Locating → Picking → Placed → Watching → Taken | Missed | Failed`  
(+ `Skipped` if the person declines)

Gates:

| Gate | Behavior |
|------|----------|
| Confirm | No arm motion until clear yes |
| OCR confidence | No motion if score &lt; threshold / ambiguous |
| Place verify | Arm cams must see successful bowl place |
| Adherence | Reachy watch and/or voice “Did you take it?” |

---

## Repository map

```text
hack_leurkerbad/
├── README.md                 ← you are here
├── AGENTS.md                 ← rules for coding agents
├── agents.local.md.example   ← copy → agents.local.md (gitignored)
├── context.md                ← full product brainstorm
├── house-help-ideas.md       ← fetch / mobility / rehab ideas
├── DEMO_CHECKLIST.md
├── .env.example              ← copy → .env (gitignored): robot IP, port, database…
├── scripts/                  ← bash helpers (install / run / install on robot)
├── .cursor/skills/           # Cursor agent skills (one per subsystem)
└── apps/medicine_buddy/      # ★ THE REACHY MINI APP (Python package + tests)
    ├── pyproject.toml        # entry point reachy_mini_apps: medicine_buddy
    ├── README.md / index.html / style.css   # app-store metadata + landing page
    ├── medicine_buddy/
    │   ├── main.py           # ★ MedicineBuddy app: conversation app + API + voice guard
    │   ├── conversation/     # voice tools + medicine_caregiver profile (bundled)
    │   ├── api.py            # HTTP API, status page /, family settings /family
    │   ├── access.py         # people, passphrases, access control, family memories
    │   ├── voice_guard.py / web_safety.py / settings.py / catalog.py
    │   ├── orchestrator.py   # DoseStateMachine
    │   ├── db.py / states.py / scheduler.py / voice_setup.py
    │   ├── ports.py          # ★ contracts for arm/vision team
    │   ├── dry_run.py        # FakeVision / FakeArm / FakeAdherence
    │   ├── vision_ocr.py / arm_controller.py   # hardware stubs
    │   └── …
    └── tests/
```

---

## How to run

Medicine Buddy is a **Reachy Mini app** (`apps/medicine_buddy`). One process runs everything:
Pollen's conversation app (listening / speaking) with the locked `medicine_caregiver` profile and our
voice tools, the Medicine Buddy API + pages on `127.0.0.1:8787`, and the voice guard.

You need Python 3.11+ (3.12 recommended), Git, and the robot on the same network as your computer.

### 1. Install (once, and after every `git pull`)

```bash
cd apps/medicine_buddy
python -m venv .venv
```

Activate it - Windows: `.venv\Scripts\activate` · macOS/Linux: `source .venv/bin/activate` - then:

```bash
pip install -e ".[dev]"
```

This also installs `reachy-mini` and Pollen's conversation app (pinned to v1.2.0).

### 2. Configure (once)

Copy `.env.example` to `.env` in the repo root and set `REACHY_HOST` to the robot's address.
On Windows `reachy-mini.local` may resolve to IPv6 and fail - use the robot's IPv4 address
(e.g. from Reachy Mini Control, or `python -c "import socket; print(socket.gethostbyname('reachy-mini.local'))"`).

### 3a. Run on the robot from Reachy Mini Control (Wireless)

Install the app into the robot's app environment (robot needs internet; the SSH password is in Pollen's
Wireless guide). Run this from your computer:

```bash
ssh pollen@reachy-mini.local "/venvs/apps_venv/bin/pip install --upgrade 'medicine_buddy @ git+https://github.com/tommasosommaruga/LosPollosForTheElders@main#subdirectory=apps/medicine_buddy'"
```

(Replace `main` with a branch name to test a branch.) Then open **Reachy Mini Control → Applications →
Medicine Buddy → Start**. Or start it over the robot's REST API:

```bash
curl -X POST http://reachy-mini.local:8000/api/apps/start-app/medicine_buddy
```

To make it start when the robot wakes up (or an antenna is touched):

```bash
curl -X PUT http://reachy-mini.local:8000/api/apps/startup-app -H "Content-Type: application/json" -d "{\"startup_app\": \"medicine_buddy\"}"
```

On the robot, the database lives in `~/.local/share/medicine-buddy/` (kept across app updates). The status
and family pages only listen on the robot itself; open them from your computer through an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 pollen@reachy-mini.local
```

then browse to http://127.0.0.1:8787/ and http://127.0.0.1:8787/family.

### 3b. Run from your computer (development, or Reachy Mini Lite)

With the venv active, from the repo root:

```bash
python -m medicine_buddy.main
```

It uses `REACHY_HOST` from `.env`; or pass it: `python -m medicine_buddy.main --host 192.168.1.42`.
Stop with Ctrl+C. Pages: http://127.0.0.1:8787/ (status) and http://127.0.0.1:8787/family (passphrases).
Make sure the app is **not** also running on the robot - only one app can drive Reachy at a time.

### Other commands

```bash
pytest                                                   # 278 tests (in apps/medicine_buddy)
medicine-buddy serve --dry-run --voice-guard             # only the API + pages, no robot
printf 'yes\nyes\nyes\n' | medicine-buddy demo --dry-run # console demo of the dose pipeline
reachy-mini-app-assistant check apps/medicine_buddy      # validate the app package
```

### What you can say to Reachy

- *"Add aspirin for Mario every day at 9 AM"* - needs **Mario's** passphrase (or the caregiver's); the
  first time, Reachy asks Mario to choose one and **say it twice**.
- *"Move Mario's aspirin to 8 PM"* / *"Stop the aspirin reminder"* (asks for confirmation).
- *"What medicines does Nonna Rosa take?"* · *"Is any medicine due?"* · *"I took it"*.
- Tell a story or recipe - Reachy asks whether to **save it in your memories**; later the grandchildren
  can ask *"Tell me a story Grandpa Mario told you"*.
- *"What's the weather in Geneva?"* - safe web search.

### Reminders, time zone and demo mode

- **Reachy reminds by itself.** A background check runs every 30 seconds. When a dose is due, Reachy
  speaks up (through the conversation app) and asks whether to bring it or whether it was already taken.
  It repeats every 10 minutes, up to 3 times. It waits until Reachy is idle: never while someone is talking to it, while Reachy is thinking or speaking, or while a tool call (e.g. a web search) is still running. If nobody answers
  within an hour the dose is recorded as **Missed** (visible on the dashboard). Switch off with
  `MEDICINE_BUDDY_REMINDERS=0`.
- **Wakes up by itself** — if Reachy is in its sleep pose when a dose is due, it runs the wake-up movement first, then speaks the reminder for that person's medicine (the app must be running; an awake robot is left alone). Turn off with `MEDICINE_BUDDY_WAKE_ROBOT=0`. The Debug page shows when it last woke.
- **Wake greeting** — when the app starts, Reachy says a short hello and which medicines are due in the next 30 minutes (real schedules). Turn off with `MEDICINE_BUDDY_WAKE_GREETING=0`.
- **Set the time zone once** on the Family settings page ("Clock & time zone", one click for your device's zone)
  or with `MEDICINE_BUDDY_TZ=Europe/Zurich`. The robot's own clock is usually UTC, so without this a 9 AM
  reminder would fire at the wrong hour.
- **"I took it" works without the arm**: say it any time and the dose is closed as Taken (or Missed).
- **Demo mode:** until the Maker Arm is wired in, dispensing is simulated. Reachy says so
  ("the arm is only simulated... please take it from your own box") instead of claiming it brought the
  medicine, and the pages show a DEMO MODE banner.

### In-app pages: dashboard, history, conversation, database, family settings, debug

When Medicine Buddy runs, Reachy Mini Control opens **`http://<robot>:7860/medicine`** (from a laptop run:
http://127.0.0.1:7860/medicine). Sign in with the **caregiver's passphrase** (typed; open while nobody is
registered; any family member's while there is no caregiver yet).

**Open it from your computer over Wi-Fi (no SSH needed):** put the laptop on the same Wi-Fi as the robot,
start Medicine Buddy in Reachy Mini Control, then browse to `http://reachy-mini.local:7860/medicine`
(or the robot's IP, e.g. `http://172.20.10.5:7860/medicine`). Not `/index.html`: that is a 404.
If the network blocks device-to-device traffic, use a phone hotspot, or forward the port over SSH
(leave the window open, then browse to http://localhost:7860/medicine):

```bash
ssh -L 7860:localhost:7860 pollen@reachy-mini.local
```

SSH is only for installing/updating the app (section 3a). Pages you see:

- **Dashboard** - people, schedules, due now, family memories, dose log, security.
- **Conversation** - live text of what the person said and what Reachy answered (every 2 s). Kept in
  memory only (last 200 lines, never in the database or logs, gone on restart); spoken passphrases are hidden.
- **Database** - read-only view of every table (people, medicines, schedules, dose events, memories,
  settings) with file path and size. Passphrases are never shown, only stored as salted hashes.
- **History** - agent activity timeline (cursor refresh), cross-entity search (people / stories /
  doses / medicines / activity), per-person dossiers, record detail pages, and adherence reports
  (7/30 days). Backed by SQLite (`activity_events` + existing tables). Person filters use **exact**
  name match only (Maria never merges with Mario). Auth-gated JSON under `/medicine/api/*`.
- **Family & passphrases** - change passphrases, suggest easy ones, add people.
- **Debug** - live view every 3 s: versions, database, voice guard (who is unlocked and for how long,
  open confirmation questions, blocked attempts), data counts, and the recent requests with their
  outcome. The **Debug mode** switch turns on detailed `medicine_buddy` logs (or set `MEDICINE_BUDDY_DEBUG=1`).
  It never shows what people said or any passphrase.
- **Voice & language settings** - Pollen's conversation settings, still at `/`.

These pages are on the local network, so they need the sign-in: 5 wrong attempts lock that device
out for a few minutes, the session cookie is HttpOnly/SameSite=Strict, and changes need a per-session
token. They're plain HTTP, so use them on a network you trust. Passphrase hashes and utterance text
are never exposed in History or Debug.

Passphrases are **changed on the Family settings page** (typed, never spoken). Medicine names are
limited to [`medicine_catalog.json`](apps/medicine_buddy/medicine_buddy/medicine_catalog.json).

### Security in one paragraph

Each person's passphrase protects their own timetable and memories (the caregiver's works for everyone);
only a salted PBKDF2 hash is stored. The **voice guard** accepts medicine changes only when the speech
recognizer heard the person themselves (medicine named, "yes" after the question, passphrase spoken in the
last 5 minutes), so text from web pages, camera images or the AI's memory can't change anything. Web
results are cleaned and labelled untrusted. The local API refuses requests from other websites and DNS
rebinding; the family page only works on the machine running the app. Known limits: reading memories and
medicines is open to anyone talking to the robot, and the conversation app's console log prints what was
said and tool arguments (which can include a spoken passphrase) - keep those logs private.

---

## HTTP API (arm PC / same host)

Default: `http://127.0.0.1:8787`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | **Status page** (open in a browser): schedules, allowed medicines, due now, dose log. Auto-refreshes. |
| GET | `/health` | Liveness |
| GET | `/medicines` | List medicines + active schedules (+ `speak_hint`) |
| GET | `/catalog` | Allowed medicine names |
| GET | `/due` | Doses due now (15 min before → 60 min after, not yet taken/skipped) |
| POST | `/schedules` | `{name, weekdays, time}` — name must match the catalog; read back + confirmed |
| POST | `/schedules/update` | `{name, [time], [new_time], [new_weekdays]}` — change a schedule; read back + confirmed |
| POST | `/schedules/delete` | `{name, confirmed, [time], [all]}` — stop reminders (soft delete, history kept) |
| POST | `/voice/heard` | `{text}` — person's speech from the launcher; **loopback only**, no AI tool calls it |
| POST | `/people/register` | `{name, role, passphrase, passphrase_repeat, [caregiver_passphrase]}` |
| POST | `/people/list` | Registered names, roles, counts (never passphrases) |
| POST | `/medicines/list` | `{[person]}` — one person's (or everyone's) medicines |
| POST | `/memories/save` | `{person, kind, title, text, [passphrase]}` |
| POST | `/memories/recall` | `{person, [kind], [topic]}` — open to family |
| POST | `/memories/forget` | `{person, memory_id, confirmed, [passphrase]}` |
| GET | `/security` | Voice guard status + recently blocked actions |
| POST | `/doses/run` | `{medicine_name, confirmed, stop_after_placed?}` |
| POST | `/doses/adherence` | `{medicine_name, taken}` |
| GET | `/medicines/{id}/events` | Dose transition log |

`speak_hint` fields are for the conversation LLM to read aloud — Medicine Buddy does not speak when using SilentBuddy.

---

## Handoff for the arm / camera / training team

You own making **real** motion and vision replace the dry-run fakes. Orchestration and safety gates stay in this repo.

### Contract to implement

File: [`apps/medicine_buddy/medicine_buddy/ports.py`](apps/medicine_buddy/medicine_buddy/ports.py)

```python
class VisionPort(Protocol):
    def locate_medicine(self, medicine_name: str) -> OcrMatch | None:
        # return OcrMatch(slot_id, label, score) or None if low/ambiguous

class ArmPort(Protocol):
    def replay_slot_to_bowl(self, slot_id: int) -> None: ...
    def verify_place(self, slot_id: int) -> PlaceVerifyResult: ...
```

Wire them in [`api.py`](apps/medicine_buddy/medicine_buddy/api.py) / CLI instead of `FakeVision` / `FakeArm` when `dry_run=false`.

### Suggested arm-team deliverables

1. **Tray calibration** — fixed N slots; map `slot_id →` grasp/approach waypoints.
2. **OCR / label read** — overhead (+ wrist assist); match known medicine names; empty slots; return **confidence**.
3. **Teach & replay** — record joint (or Cartesian) trajectories per slot → bowl; store under `apps/medicine_buddy/data/waypoints/` (or your format; adapt `arm_controller.py`).
4. **Place verify** — after place: bowl occupied / slot cleared heuristics from the two cameras.
5. **Safety** — e-stop reachable; soft limits; refuse motion if OCR score &lt; `ocr_min_confidence` (config).

### Hardware references

- Maker Arm SDK: https://github.com/makermods-robotics/maker-arm-sdk  
- MakerMods Lab / training: https://www.makermods.ai/maker-arm  
- Cameras: wrist + overhead over USB on the arm host  

### Out of scope for arm MVP (unless you finish early)

Opening blister/plastic · pill counting · multi-room chores · Dream-1 as primary policy (optional later fallback)

---

## Safety (Always / Never)

**Always**

- Confirm with the person before every dispense move  
- OCR confidence gate  
- Persist every state transition  
- Prefer dry-run until hardware verified  

**Never**

- Medical / dosing advice  
- Open packaging in MVP  
- Move arm on low OCR confidence  
- Lift people / hot liquids / sharps  

See [`DEMO_CHECKLIST.md`](DEMO_CHECKLIST.md) and [`AGENTS.md`](AGENTS.md).

---

## Roadmap vs MVP

| Area | MVP (this repo + arm team) | Later (from context.md) |
|------|----------------------------|-------------------------|
| Schedule DB + voice | Yes | Recurrence UX polish |
| Remind + confirm + dispense | Yes | Stronger vision adherence |
| Caregiver alert on Missed | Yes (ntfy / webhook) | SMS / email gateways |
| Adaptive self-care first | Yes | Graded independence profiles |
| Companion check-ins + memories | Yes | Language practice / games |
| Family video-call link | Yes (URL bridge) | Embedded WebRTC |
| Soft motor / cognitive prompts | Yes (voice) | Arm rehab reps (arm team) |
| OCR + waypoints → bowl | Arm team | VLA / Dream-1 fallback |
| Open packaging | No | Maybe supervised |
| Smart home / scam calls / help | No | Companion platform |
| Multi-arm chores | No | House fleet |

Fetch/bring and arm rehab demos remain in [`house-help-ideas.md`](house-help-ideas.md) for the arm team after the meds loop is live.

---

## Training agents & new teammates

1. Read this README + [`AGENTS.md`](AGENTS.md) + `agents.local.md` (create from example).  
2. Skim Cursor skills under [`.cursor/skills/`](.cursor/skills/) — one skill per subsystem.  
3. Run dry-run demo + `pytest` in `apps/medicine_buddy`.  
4. Trace one dose: `orchestrator.py` → ports → `api.py` → conversation tools.  
5. Arm teammates: implement `VisionPort` / `ArmPort`; keep state machine untouched.

Upstream Reachy agent guide (patterns we mirror):  
https://github.com/pollen-robotics/reachy_mini/blob/main/AGENTS.md

---

## Status (v0.1)

| Piece | Status |
|-------|--------|
| SQLite schedule + dose events | Done |
| Dose state machine + dry-run demo | Done |
| HTTP API + conversation external tools/profile | Done |
| Pull-and-run scripts for robot host | Done |
| Real Maker Arm SDK replay | Stub — **arm team** |
| Real dual-cam OCR | Stub — **arm team** |
| Live Reachy TTS inside this package | Not used — **pollen conversation app** |
| Caregiver notify / smart home / memory | Out of MVP |

---

## License / credit

Hackathon prototype. Reachy Mini and conversation app © pollen-robotics. Maker Arm © MakerMods. Product vision and elderly-care framing from the team notes in this repo.
