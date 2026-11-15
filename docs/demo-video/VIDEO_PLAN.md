# Medicine Buddy — Demo Video Plan

A ~2:30 tech demo of Reachy Mini + Maker Arm as a home companion for an older person.
Tone: quiet, warm, human-first — the calm, documentary feel of Claude's YouTube films,
not a loud product ad. (It's *inspired by* that style: use your own name and branding,
no Anthropic/Claude logos or fonts that would make it look like an official Anthropic video.)

Contents: [1. Style guide](#1-style-guide) · [2. Feature list](#2-feature-list-what-the-product-does) ·
[3. Script](#3-script) · [4. Shot list](#4-shot-list) · [5. Editing with Claude](#5-editing-with-claude) ·
[6. Pre-shoot checklist](#6-pre-shoot-checklist)

---

## 1. Style guide

| Element | Do | Avoid |
|---|---|---|
| **Pace** | Long holds (3–6 s), straight cuts, a few slow cross-dissolves | Whip pans, glitch transitions, speed ramps |
| **Light** | Soft window light, morning gold, practical lamps in frame | Ring lights, RGB, blue "tech" glow |
| **Colour** | Warm neutrals: cream `#F0EEE6`, ink `#191919`, clay accent `#C96442` (one accent only) | Neon, gradients, saturated blues |
| **Framing** | Lots of negative space, subject off-centre, shallow depth of field, hands & faces | Robot-only spec shots with no person |
| **Type** | One serif for titles (e.g. *Source Serif*, *Newsreader*), one clean sans for labels (*Inter*). Small, centred or bottom-left | Bold all-caps, kinetic typography |
| **Sound** | Real room tone, birds/kettle/clock, sparse piano or soft acoustic guitar, lots of silence | EDM, whooshes, beeps on every cut |
| **Voice** | Calm narrator *or* no narrator — let Mario and Reachy carry it | Hype phrases ("revolutionary", "game-changer") |
| **Text cards** | Plain cream full-screen card, one short sentence, 2–3 s | Bullet lists on screen |
| **Robot** | Filmed like a character: eye-level, reacting, antennas moving | Stiff product turntable as the hero shot |

Story rule: **every feature is shown through a moment in Mario's day**, never as a list.

Camera settings: 4K 24fps (25fps in Europe), 180° shutter (1/50), shoot log/flat if available,
lock white balance (~4800K for warm window light). Shoot B-roll details at 50/60fps for gentle slow-mo.

---

## 2. Feature list (what the product does)

Grounded in the code in `apps/medicine_buddy` (v0.8.8). ★ = hero features for the video.

### Medicine care
- ★ **Voice setup of medicines** — "Add Aspirin for Mario every day at 9 AM." Read back and confirmed. Only names from an allowed catalog (Swissmedic-derived) are accepted.
- **Change / stop schedules by voice** — "Move my Aspirin to 8 PM", "Stop the Vitamin D reminder" (asks for confirmation).
- ★ **Proactive reminders** — Reachy speaks up when a dose is due, waits until nobody is talking, repeats every 10 min up to 3 times, logs **Missed** after an hour.
- ★ **Wake greeting** — on start-up Reachy says hello and what's due in the next 30 minutes.
- ★ **Self-care first** — Reachy encourages Mario to take the box himself; offers the arm only if he asks or can't reach.
- ★ **Arm dispense with safety gates** — confirm-before-move → dual-camera OCR finds the labelled box → confidence gate → taught motion slot → bowl → cameras verify the place.
- ★ **Adherence** — "Did you take it?" / "I took it" closes the dose as Taken or Missed.
- ★ **Caregiver alert** — a Missed dose pushes a notification to the family's phone (ntfy / webhook).
- **Dose state machine** persisted to SQLite: Due → Reminded → Confirmed → Locating → Picking → Placed → Watching → Taken / Missed / Failed.

### Companion & family
- ★ **Family memories** — Mario tells a story or recipe; Reachy asks whether to save it. Later a grandchild asks *"Tell me a story Grandpa Mario told you."*
- ★ **Read a letter** — Mario holds a paper to Reachy's camera; Reachy coaches him on how to hold it, then reads it with OCR.
- ★ **Family video call** — "Call my family" opens the saved Meet / FaceTime / WhatsApp / Zoom link.
- **Activity suggestions** — gentle optional stretch, short walk, memory chat (logged as accepted/declined for caregivers). Not therapy.
- **Idle check-ins** — when it's quiet, Reachy gently asks how he's doing; respects quiet hours.
- **Companion calendar** — doctor visits, family visits; "What's on today?"; ICS import/export.
- **Safe web search** — "What's the weather in Geneva?" (results treated as untrusted).

### Trust & privacy
- ★ **Personal passphrases by voice** — each person's medicines and memories are protected; only salted hashes stored.
- ★ **Voice guard** — medicine changes only count if the *person actually said them* — not text from a web page, camera image or the AI's memory.
- **Never gives dosing advice, never opens packaging, never moves the arm without a clear yes.**

### Caregiver web app (`:7860/medicine`)
- ★ **Dashboard** — people, schedules, due now, dose log, DEMO MODE banner when the arm is simulated.
- **Conversation** — live transcript (memory only, passphrases hidden).
- **History** — activity timeline, search, per-person dossiers, 7/30-day adherence reports.
- **Database** — read-only view of every table. **Family & passphrases**, **Debug** pages.

---

## 3. Script

Characters: **Mario** (80s, lives alone — a real grandparent if you can, consent signed), **Giulia** (daughter, caregiver, in another city), **Luca** (grandchild, ~10). **Reachy** speaks with its own TTS voice. Narration is optional — marked `VO`. Many people prefer the no-VO version; the scene works with on-screen text only.

> Reachy's replies come from a live LLM, so they won't be word-perfect. Film real interactions, do several takes, and pick the closest. Lines below are the target.

**Runtime target: 2:30**

### 0:00 – 0:15 · Cold open — "Morning"
*Silence, then room tone. Kettle. Clock ticking.*

Shots 1–4. Mario's hand pours tea. A calendar on the wall. Medicine boxes on a shelf, slightly out of reach. Reachy on the kitchen table, head down, asleep.

CARD (cream, serif): **Most of us want to grow old at home.**

### 0:15 – 0:30 · Wake
Reachy's head lifts, antennas rise.

> **REACHY:** Good morning, Mario. Your Aspirin is due at nine.

> **MARIO** *(smiles)*: Good morning to you too.

CARD: **Medicine Buddy** — *a companion that remembers, so you don't have to.*

### 0:30 – 0:50 · Setting up, by voice
*Flashback-feel, slightly different light: Giulia sits beside Mario the first day.*

> **MARIO:** Add Vitamin D for me, every day at nine.
>
> **REACHY:** First, choose a passphrase only you know. Say it twice.
>
> **MARIO** *(leans in, whispering — audio ducked/blurred)*: …
>
> **REACHY:** Vitamin D, every day at 9 AM, for Mario. Is that right?
>
> **MARIO:** Yes.

VO *(optional)*: "No apps. No buttons. Just a conversation — and only the voice that owns the schedule can change it."

### 0:50 – 1:25 · The reminder → the arm (hero sequence)
Clock ticks to 9:00. Reachy looks up toward Mario.

> **REACHY:** Mario, it's time for your Aspirin. Can you reach the box, or shall I bring it?
>
> **MARIO** *(glances at the high shelf / his knee)*: Could you bring it?
>
> **REACHY:** I'll bring your Aspirin to the bowl. Shall I go ahead?
>
> **MARIO:** Yes, please.

*Music enters softly.* Macro: overhead camera view, label "ASPIRIN" highlighted, confidence score ticks up. Arm moves — slow, deliberate. Gripper closes on the box. Box lands in the bowl. Over-the-shoulder: Mario takes it.

On-screen labels (small sans, bottom-left, one at a time):
`Confirmed by voice` → `Label read · 0.97` → `Placed · verified`

> **REACHY:** Did you take it?
>
> **MARIO:** I took it. Thank you.
>
> **REACHY:** Logged. Have a good morning.

CARD: **It asks first. Every time.**

### 1:25 – 1:40 · When something's missed
*Cut to Giulia at work in another city.* Her phone buzzes on the desk.

Phone screen insert: **"Mario — Vitamin D at 9:00 was missed."**

She opens the dashboard on her laptop — adherence history, today's log. She calls him.

VO *(optional)*: "Family stays in the loop — without hovering."

### 1:40 – 2:05 · Memories
*Afternoon light.* Mario is talking, hands moving, telling a story about his first bicycle / his mother's polenta.

> **REACHY:** That's a lovely story. Would you like me to save it in your memories?
>
> **MARIO:** Yes — for Luca.

*Match cut* — days later: Luca sits in front of Reachy.

> **LUCA:** Tell me a story Grandpa Mario told you.
>
> **REACHY:** He told me about the red bicycle he rode to school in 1952…

Luca laughs. Hold on his face.

### 2:05 – 2:20 · A day, together (montage, 1–2 s each)
- Mario holds a letter up to Reachy: *"Hold it a little closer… a bit more light… got it."*
- Reachy suggests a stretch; Mario stands, arms up, grinning.
- "Call my family" → laptop opens a video call; Giulia waves.
- "What's on today?" → "Dr. Rossi at 3 PM."

### 2:20 – 2:30 · Close
Evening. Lamp on. Mario says goodnight; Reachy's head lowers.

CARD (three lines, appear one at a time):
**Reminds. Asks. Remembers.**
**Never gives medical advice. Never moves without a yes.**
**Medicine Buddy — Los Pollos For The Elders**

*Small credit line:* Built on Reachy Mini (Pollen Robotics) and the Maker Arm (MakerMods).

---

## 4. Shot list

**Gear:** main camera + 35mm and 85mm (or a zoom), a macro or close-focus lens, small slider or gimbal (optional), tripod, lav mic on Mario, shotgun mic for Reachy (record Reachy's speaker from 30 cm), OBS for screen recordings, a phone for the notification insert.

**Codes:** WS wide · MS medium · CU close-up · ECU extreme close-up · OTS over-the-shoulder · POV · INSERT · SCR screen recording.
**File naming on set:** `S##_T##` (shot, take). Slate verbally: "Shot 14, take 2."

| # | Scene | Shot | Framing / movement | Lens | What to capture | Notes |
|---|---|---|---|---|---|---|
| 1 | Cold open | WS kitchen | Static, tripod, window in frame | 35 | Empty morning kitchen, light through window | 10 s hold, room tone |
| 2 | Cold open | ECU | Static | Macro | Tea pouring, steam in backlight | 50/60 fps |
| 3 | Cold open | INSERT | Slow slider push | 85 | Medicine boxes on high shelf | Shows "hard to reach" |
| 4 | Cold open | MS | Static, low angle | 35 | Reachy asleep, head down | Shallow DOF |
| 5 | Wake | CU | Static, eye-level with Reachy | 85 | Head lifts, antennas rise | Several takes; trigger via app start |
| 6 | Wake | MS OTS Reachy | Static | 35 | Mario reacts, smiles | Mario's line |
| 7 | Setup | 2-shot | Handheld, gentle | 35 | Giulia + Mario at table, Reachy facing them | Warmer/softer grade later = "first day" |
| 8 | Setup | CU | Static | 85 | Mario leaning in to whisper passphrase | Don't record a real passphrase |
| 9 | Setup | CU | Static | 85 | Reachy "listening" — head tilt | Cutaway |
| 10 | Reminder | INSERT | Static | Macro | Wall clock hitting 9:00 | Set the clock manually |
| 11 | Reminder | MS | Static | 35 | Reachy turns toward Mario, speaks | Use force-trigger / short schedule |
| 12 | Reminder | CU | Static | 85 | Mario glances at shelf, rubs knee | Motivation for the arm |
| 13 | Arm | WS | Static, wide enough for arm + bowl + Mario | 24–35 | Full dispense move start→finish | **Hero shot** — e-stop in reach, out of frame |
| 14 | Arm | SCR / camera feed | Screen capture | — | Overhead cam view + OCR box + confidence | Record arm camera feed directly |
| 15 | Arm | ECU | Slider, tracking | Macro | Gripper closing on labelled box | 50/60 fps |
| 16 | Arm | ECU | Static, top-down | 35 | Box landing in the bowl | 50/60 fps |
| 17 | Arm | OTS Mario | Static | 85 | Mario reaches into bowl, takes box | |
| 18 | Adherence | CU | Static | 85 | "I took it. Thank you." | |
| 19 | Arm | CU | Static | 85 | Reachy nods / antennas move after "Logged" | |
| 20 | Missed | MS | Static | 35 | Giulia at desk (different location / cooler light) | Different city feel |
| 21 | Missed | INSERT | Static, phone on desk | Macro | Phone buzzes, ntfy notification | Real alert from the app |
| 22 | Missed | SCR | OBS 1080p+ | — | Dashboard → History → adherence report | Use clean demo data |
| 23 | Missed | MS | Static | 35 | Giulia picks up phone to call | |
| 24 | Memories | MS | Slow push-in | 35→50 | Mario telling the story, hands moving | Let him talk for real, 2–3 min |
| 25 | Memories | CU | Static | 85 | Reachy listening, slight head tilts | Cutaways |
| 26 | Memories | INSERT | Static | Macro | Old photo of the bicycle / recipe card | Optional prop |
| 27 | Memories | MS | Static, same framing as 24 | 35 | Luca in Mario's seat asking for the story | **Match cut** with 24 |
| 28 | Memories | CU | Static | 85 | Luca laughing | |
| 29 | Montage | MS | Static | 35 | Mario holds letter to Reachy, Reachy coaches | `read_document` |
| 30 | Montage | WS | Static | 24 | Mario stands and stretches | `suggest_activity` |
| 31 | Montage | OTS | Static | 50 | Video call opens on laptop, Giulia waves | `start_family_call` |
| 32 | Montage | CU | Static | 85 | "What's on today?" — Reachy answers | Calendar |
| 33 | Close | WS | Static | 35 | Evening, lamp on, Mario says goodnight | Golden/blue hour |
| 34 | Close | CU | Static | 85 | Reachy's head lowers to sleep | Final image |
| B1–B10 | B-roll | — | Mixed | Macro/85 | Antennas moving, Reachy's eyes, labelled box texture, bowl, hands on table, window light, steam, plants | Grab plenty; it saves the edit |
| SCR1–5 | Screens | SCR | OBS | — | Dashboard, Conversation live transcript, History timeline, Family page, Debug/voice guard | Hide real passphrases & personal data |

**Audio to record separately:** 60 s room tone in each location; Reachy saying each key line cleanly (in case on-set audio is noisy); arm motor sound close-up.

---

## 5. Editing with Claude

Claude doesn't replace your editor (DaVinci Resolve is free and great for this), but Claude Code can do the slow parts:
logging, take selection, rough-cut assembly, captions, title cards and review notes.

### Folder layout
```
video/
  footage/        S01_T01.mp4 … (camera files renamed to shot/take)
  audio/          room tone, clean Reachy lines
  screens/        OBS recordings
  transcripts/    auto-generated
  graphics/       rendered title cards / labels
  frames/         review stills
  edit/           timelines, EDLs, captions
```

### Step 1 — Rename & log footage
Prompt to Claude Code (in the `video/` folder):
> "Here's my shot list (paste section 4). The files in footage/ are in shooting order. I'll tell you which shot each clip is; rename them to S##_T## and make a log CSV with duration, resolution and fps using ffprobe."

### Step 2 — Transcribe everything
Run a local speech-to-text tool such as `faster-whisper` or `whisper.cpp` on every clip to get `.srt`/`.json` with timestamps. Claude Code can write and run the batch script for you:
> "Write a script that transcribes every clip in footage/ to transcripts/ as SRT with word timestamps."

### Step 3 — Pick takes & build the rough cut
> "Using the script (paste section 3), the shot list and the transcripts, pick the best take for each line (cleanest delivery, closest to the script, no talking over Reachy). Output an edit decision list as edit/timeline.json with clip, in/out timecodes and a reason. Then generate an .fcpxml (or OpenTimelineIO .otio) I can import into DaVinci Resolve."

Import the `.fcpxml` in Resolve → *File → Import → Timeline*. You now have a rough cut in minutes instead of hours.

### Step 4 — Title cards & on-screen labels
**Already built:** every title card, label, name tag and time stamp in this plan is rendered in
[graphics/out/](graphics/out/) (16:9 and 9:16), with placement notes in
[graphics/README.md](graphics/README.md). To change wording, ask Claude Code to edit
`graphics/src/graphics.html` and re-run `graphics/src/render.py`.

To make new ones from scratch, ask Claude to build the graphics as HTML/CSS (or a small Remotion project) using the style guide, then render to PNG/ProRes 4444 with alpha:
> "Make 1920×1080 title cards on #F0EEE6 with ink #191919 text in Source Serif, centred, generous margins, for these lines: … Also make bottom-left lower labels in Inter 28px with a 4px clay #C96442 dot: 'Confirmed by voice', 'Label read · 0.97', 'Placed · verified'. Render each to graphics/ as transparent PNGs, plus a 12-frame fade-in version."

### Step 5 — Captions
> "Merge the selected takes' transcripts into one SRT that matches timeline.json timing. Fix spelling of Reachy, Mario, Aspirin, Vitamin D; max 2 lines, 42 chars per line; remove the passphrase line."

### Step 6 — Review loop (Claude watches stills)
Export a low-res draft, then:
> "Extract one frame every 2 seconds from draft_v1.mp4 into frames/ with ffmpeg, look at them alongside the script, and give me notes: pacing, shots that feel too long, framing problems, places where text is unreadable, and anything that breaks the calm style guide."

Claude Code can read images, so it can comment on framing, consistency of colour and text legibility. Repeat for each version.

### Step 7 — Finish in Resolve
- **Colour:** warm, slightly lifted blacks, low saturation, keep skin natural; one consistent look across scenes; the "first day" flashback a touch softer and warmer.
- **Sound:** dialogue −12 dB peaks, music under dialogue at −24 to −28 dB, room tone under every cut, fade music out before the final card.
- **Export:** H.264/H.265, 4K or 1080p, 24/25 fps, −14 LUFS for YouTube. Also export a 60 s vertical cut (shots 5, 11, 13, 16, 21, 27, final card) for social.

---

## 6. Pre-shoot checklist

- [ ] Signed consent from everyone on camera (especially Mario and any child).
- [ ] App running (`medicine-buddy` on the robot), time zone set on the Family page.
- [ ] Clean demo database: Mario, Giulia (caregiver), Luca; Aspirin + Vitamin D schedules; one saved story; one calendar event ("Dr. Rossi, 3 PM"); family call link set.
- [ ] **Arm:** if the real Maker Arm isn't wired yet, the app is in DEMO MODE and Reachy will say the arm is simulated. Either film after the arm team connects it, or film the arm move separately with its own teach/replay and label that sequence honestly ("prototype"). Don't present simulated steps as real.
- [ ] Labelled boxes with large text in the tray; bowl in its fixed position; e-stop in reach (out of frame).
- [ ] Caregiver alert URL configured (ntfy) and Giulia's phone subscribed, for the real notification shot.
- [ ] Use dummy passphrases; hide them in every screen recording; no real personal or medical data on screen.
- [ ] Reminder trigger ready for shot 11 (a schedule a minute ahead, or the API).
- [ ] Batteries, cards, room-tone recordings in every location.
