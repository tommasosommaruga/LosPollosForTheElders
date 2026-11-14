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

Help an older person live safer and more independently at home:

1. **Medication care** — know *what* to take and *when*; remind gently; bring the correct closed box; check they took it.
2. **Safe fetch & bring** — reduce bending/reaching (phone, water, remote, glasses) — see house-help ideas.
3. **Companion** — conversation, memory (with consent), language practice, games, water/appointment reminders, caregiver alerts, help calls / scam call filtering, smart-home — longer roadmap in [`context.md`](context.md).

We are **not** a doctor. No dosing advice, no opening packaging in the MVP, no lifting people.

### Demo MVP (what this codebase targets now)

End-to-end **medicine reminder + dispense + adherence**:

1. Person (by voice via Reachy) stores usual pills and recurrence → **SQLite schedule**.
2. At due time (or force-trigger), Reachy says: *Time for your {name} — should I bring it?*
3. On **yes**, dual arm cameras + OCR find the **standardized labeled box** (empty slots allowed).
4. Arm replays taught motion: **slot → bowl** (bowl preferred over plate for detection).
5. Arm cams **place-verify**; Reachy cam / voice confirms **taken** or logs **missed**.

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
├── scripts/
│   ├── bootstrap.sh
│   ├── run_robot_host.sh              # starts API + writes .env.generated
│   └── run_conversation_with_medicine.sh
├── .cursor/skills/           # Cursor agent skills (one per subsystem)
├── integrations/reachy_conversation/
│   ├── README.md
│   ├── external_tools/       # pollen external Tool modules
│   ├── external_profiles/medicine_caregiver/
│   └── lib/tool_http.py
└── apps/medicine_buddy/      # Python package + tests
    ├── medicine_buddy/
    │   ├── api.py            # HTTP for conversation tools
    │   ├── orchestrator.py   # DoseStateMachine
    │   ├── db.py / states.py / scheduler.py / voice_setup.py
    │   ├── ports.py          # ★ contracts for arm/vision team
    │   ├── dry_run.py        # FakeVision / FakeArm / FakeAdherence
    │   ├── silent_buddy.py   # no TTS — conversation speaks
    │   ├── vision_ocr.py     # hardware stub
    │   ├── arm_controller.py # waypoint load + SDK stub
    │   └── …
    └── tests/
```

---

## Quick start

### Local dry-run (no robots)

```bash
cd apps/medicine_buddy
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
printf 'yes\nyes\nyes\n' | medicine-buddy demo --dry-run
pytest
```

### Robot host PC (Reachy connected — pull and run)

```bash
git pull
./scripts/bootstrap.sh                         # once
./scripts/run_robot_host.sh                    # terminal 1 — API :8787
./scripts/run_conversation_with_medicine.sh    # terminal 2 — pollen TTS
```

Requires pollen [`reachy_mini_conversation_app`](https://github.com/pollen-robotics/reachy_mini_conversation_app) on PATH as `reachy-mini-conversation-app` (or set `REACHY_CONVERSATION_CMD`).

Voice tools: `schedule_medicine` · `list_medicines` · `check_due_medicines` · `dispense_medicine` · `mark_dose_taken`

More: [`integrations/reachy_conversation/README.md`](integrations/reachy_conversation/README.md)

---

## HTTP API (arm PC / same host)

Default: `http://127.0.0.1:8787`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/medicines` | List medicines + schedules |
| GET | `/due` | Schedules due this minute |
| POST | `/schedules` | `{name, weekdays, time}` |
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
| Remind + confirm + dispense | Yes | Caregiver SMS / web glance |
| OCR + waypoints → bowl | Arm team | VLA / Dream-1 fallback |
| Adherence watch | Voice + stub cam | Stronger Reachy vision |
| Open packaging | No | Maybe supervised |
| Life memory / language / games | No | Buddy features |
| Smart home / scam calls / help | No | Companion platform |
| Multi-arm chores | No | House fleet |

Fetch/bring and rehab assist ideas remain in [`house-help-ideas.md`](house-help-ideas.md) for post-meds demos.

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
