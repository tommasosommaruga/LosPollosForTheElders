# Los Pollos For The Elders — Medicine Buddy

**Hackathon Track 4: Elderly Care & Rehabilitation** (daily help, rehab, mobility)

**Demo video:** [https://youtu.be/vsHM2_IKy6Y](https://youtu.be/vsHM2_IKy6Y)

A home assist system that pairs:

| Role | Hardware | Job |
|------|----------|-----|
| **Buddy** | [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) | Talk, remind, confirm, adherence (TTS/STT via pollen conversation app) |
| **Arm** | [MakerMods Maker Arm](https://www.makermods.ai/maker-arm) | Dual-cam locate + pick labeled objects → place in a **bowl** |

**Team split:** this repo owns scheduling, safety state machine, Reachy conversation tools, and HTTP orchestration. Arm motion / dual-cam pickup lives under [`arm/`](arm/) and talks over the `medicine_buddy.arm.v1` HTTP contract ([`docs/ARM_TEAM_HANDOFF.md`](docs/ARM_TEAM_HANDOFF.md)).

Notes: [`context.md`](context.md) · [`house-help-ideas.md`](house-help-ideas.md) · [`AGENTS.md`](AGENTS.md) · [`STATUS.md`](STATUS.md)

---

## What we built

Help an older person live safer at home across three pillars:

1. **Chronic care (medicines)** — voice schedule, gentle reminders, confirm-before-move dispense, Taken / Missed logging (optional caregiver alert).
2. **Motor autonomy** — arm assist for fetch/bring when the person cannot safely reach; prefer self-care first.
3. **Cognitive / emotional + family** — companion chat, consented memories, calendar sync; family video call is a coming-soon mock.

We are **not** a doctor. No dosing advice, no opening packaging in the MVP, no lifting people.

### Demo loop

1. Voice stores pills + recurrence → **SQLite**.
2. At due time Reachy reminds; on clear **yes**, Buddy POSTs `arm.v1` dispense to the arm PC.
3. Dual cams + vision find the labeled object; arm picks → **bowl**; place-verify.
4. Voice / UI closes the dose as **Taken** or **Missed**.

**Locked choices:** confirm-before-move · OCR / confidence gate · taught / planned grasp (not VLA for MVP) · Reachy TTS/STT stays on pollen’s conversation app.

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│ Robot host (Reachy + Medicine Buddy app)                     │
│  conversation app (TTS/STT/LLM + tools) ──▶ API :8787        │
│  schedule · dose state machine · voice guard · UI            │
└───────────────────────────────┬──────────────────────────────┘
                                │ HTTP medicine_buddy.arm.v1
                                ▼
                         Arm PC (:9090) + Maker Arm + 2 cameras
                         (see arm/ and docs/ARM_TEAM_HANDOFF.md)
```

**Dose states (persisted):**  
`Idle → Due → Reminded → Confirmed → Locating → Picking → Placed → Watching → Taken | Missed | Failed`  
(+ `Skipped` if declined)

| Gate | Behavior |
|------|----------|
| Confirm | No arm motion until clear yes |
| Confidence | No motion if score &lt; threshold / ambiguous |
| Place verify | Arm cams check bowl place |
| Adherence | Reachy watch and/or voice “Did you take it?” |

---

## Repository map

```text
hack_leurkerbad/
├── README.md · AGENTS.md · STATUS.md · .env.example
├── agents.local.md.example   → copy to agents.local.md (gitignored)
├── scripts/                  bootstrap / run / install_on_robot
├── docs/                     ARM_TEAM_HANDOFF, plans, demo-video
├── integrations/             arm listener stub · Reachy conversation helpers
├── arm/                      Maker Arm demo stack (vision + grab + dispense server)
└── apps/medicine_buddy/      ★ Reachy Mini app (Python package + tests)
    ├── medicine_buddy/main.py          conversation + API + voice guard
    ├── medicine_buddy/conversation/    tools + medicine_caregiver profile
    ├── medicine_buddy/orchestrator.py  DoseStateMachine
    ├── medicine_buddy/arm_protocol.py  arm.v1 JSON schema
    ├── medicine_buddy/http_arm.py      HTTP client to arm PC
    └── tests/
```

---

## How to run

Python 3.11+ (3.12 recommended). Medicine Buddy is one process: conversation + API (`127.0.0.1:8787`) + voice guard.

### Install (once / after pull)

```bash
./scripts/bootstrap.sh
# or manually:
cd apps/medicine_buddy && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Copy `.env.example` → `.env` and set `REACHY_HOST` (prefer robot **IPv4** on Windows).  
Optional live arm: `MEDICINE_BUDDY_ARM_URL=http://<arm-pc-ip>:9090`.

### Run from your computer

```bash
./scripts/run_robot_host.sh
# or: python -m medicine_buddy.main --host <robot-ip>
```

Pages: http://127.0.0.1:7860/medicine · API/status http://127.0.0.1:8787/  
Only one app may drive Reachy at a time (stop Control apps if developing locally).

### Install on Wireless robot

```bash
./scripts/install_on_robot.sh
# then Reachy Mini Control → Applications → Medicine Buddy → Start
```

Robot DB: `~/.local/share/medicine-buddy/`. UI on the LAN: `http://<robot>:7860/medicine` (or SSH tunnel `7860` / `8787`).

### Smoke commands

```bash
cd apps/medicine_buddy && source .venv/bin/activate
pytest -q
medicine-buddy demo --dry-run
reachy-mini-app-assistant check apps/medicine_buddy
```

### What you can say

- *"Add aspirin for Mario every day at 9 AM"* (person / caregiver passphrase when enabled)
- *"Should I bring your aspirin?"* → clear yes → arm dispense (or FakeArm if `ARM_URL` empty)
- *"I took it"* · *"Is any medicine due?"* · save / recall family memories · safe web search

Set **time zone** once on the Family page (`MEDICINE_BUDDY_TZ=Europe/Zurich`). Reminders interrupt by default; `MEDICINE_BUDDY_REMINDER_INTERRUPT=0` = polite idle-only mode.

**In-app pages (caregiver sign-in):** Dashboard · Conversation · Database · History · Family · Debug.

---

## HTTP API (Buddy)

Default: `http://127.0.0.1:8787`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` · `/health` | Status page · liveness |
| GET | `/medicines` · `/catalog` · `/due` | Schedules, catalog, due window |
| POST | `/schedules` · `/update` · `/delete` | Voice-backed schedule CRUD |
| POST | `/doses/run` · `/doses/adherence` | Confirm dispense · Taken/Missed |
| POST | `/people/*` · `/memories/*` | Access + family memories |
| GET | `/security` | Voice guard status |

Arm contract (Buddy → arm PC): see [`docs/ARM_TEAM_HANDOFF.md`](docs/ARM_TEAM_HANDOFF.md) — `POST /v1/dispense`, `/v1/move_object`, `/v1/check_taken`, job status / release.

---

## Safety

**Always:** confirm before dispense · confidence gate · persist every transition · prefer FakeArm until `ARM_URL` is set and verified  

**Never:** dosing advice · open packaging · move on low confidence · lift people / hot liquids / sharps  

See [`DEMO_CHECKLIST.md`](DEMO_CHECKLIST.md) and [`AGENTS.md`](AGENTS.md).

---

## Status (current)

| Piece | Status |
|-------|--------|
| Reachy Mini app + voice tools + UI | Done |
| SQLite schedule + dose state machine | Done |
| Confirm → HTTP `arm.v1` dispense / move | Done |
| Google Calendar + memories + reminders | Done |
| Maker Arm dual-cam grab → bowl (`arm/`) | Demo working path |
| Camera adherence (auto Taken) | Partial / voice fallback |
| Caregiver Missed push (ntfy/webhook) | Partial |
| Family video call | Coming-soon mock |

---

## License / credit

Hackathon prototype. Reachy Mini and conversation app © pollen-robotics. Maker Arm © MakerMods. Product vision from team notes in this repo.
