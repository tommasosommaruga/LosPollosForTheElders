# House help ideas — MakerMods arm (elderly care)

Context: robotic arm for training + actuated arm for real assist. Goal: safely pick/bring things and help an older person at home (Track 4: daily help, rehab, mobility).  
Hardware ref: https://www.makermods.ai/maker-arm

---

## 1. Daily help

### Fetch & bring
- Pick up a phone / remote / glasses from a table and hand them over
- Bring a water bottle or cup from counter to seated person
- Fetch medication box / pill organizer (closed container only — no dosing)
- Bring a book, newspaper, or tablet from a shelf within safe reach
- Retrieve dropped keys, wallet, or small pouch from floor (if height + grip allow)
- Bring a tissue box or napkin pack
- Fetch headphones / hearing-aid case (not the aid itself if fragile)
- Bring charging cable / power bank to the person
- Pass a pen and notepad for writing notes / lists
- Bring a favorite blanket or throw from a nearby chair

### Kitchen / meal adjacent (no cooking, no hot liquids)
- Move a sealed snack pack from cupboard edge to table
- Bring a plate or bowl that is already plated (cold / room temp only)
- Clear empty packaging into a bin if within arm workspace
- Pass utensils already laid out on the counter
- Bring a lunchbox or yogurt cup from fridge door (if fridge is pre-opened / open-door demo)
- Slide a cutting board or tray closer on the table (push/pull, not lift heavy)

### Tidying & clutter
- Put remote back on a known “home spot”
- Return glasses to a glasses stand
- Place phone on a charging dock
- Collect light trash (wrappers, tissues) into a bin
- Straighten a small object row on a side table (books, remotes)
- Close an open drawer that was left ajar (gentle push)
- Hang a light cloth / towel on a low hook

### Communication / routine support
- Bring a “call caregiver” button / pendant within reach
- Hand over a written daily checklist card
- Bring a calendar or schedule sheet to the person
- Place a reminder object (e.g. “take walk” card) in a visible spot

---

## 2. Mobility support

### Reduce bending & reaching
- Pick objects off the floor so the person does not bend
- Reach mid-shelf items the person cannot safely stretch for
- Hand items across a gap (bed ↔ table, chair ↔ counter) without standing
- Move a walker / cane closer if it rolled out of reach (light reposition, not full lift of heavy frames)
- Bring shoes or slippers to seated height
- Pass a jacket / scarf from a chair back to the person

### Within-reach workspace
- Keep a “hot zone” tray stocked: water, phone, remote, tissues, meds box
- Restock that tray from a nearby shelf on request
- Reposition the tray itself closer / farther on the table
- Clear obstacles from the path of a walker in the arm’s workspace
- Open / close a lightweight door or cupboard within safe torque limits

### Transfer-adjacent (assist objects only — never lift people)
- Hold a bag while the person sits or stands with caregiver help
- Stabilize a light table object while someone uses the table for balance (demo only; prefer not load-bearing)
- Hand the person their cane at the exact moment they need it after sitting
- Bring a portable grab-rail cushion / seat pad into place on a chair

### Night / low-energy scenarios
- Bring water to bedside without the person getting up
- Fetch a flashlight / night light from a drawer
- Bring socks or a robe from a nearby chair
- Hand over the phone if they need to call someone at night

---

## 3. Rehab / light therapy assist

Always supervised. Arm is a coach / dispenser of tasks, not a physiotherapist replacement.

### Reach & place drills
- Pick → place object from A to B (left/right, near/far)
- Graded distances: short reach → medium → longer reach
- Height variation: table → slightly higher shelf (safe range only)
- Color / shape sorting of soft foam blocks
- Stack light cups or cones in order
- Place objects into marked zones (tape targets on table)

### Paced handoffs
- Slow handoff of a soft ball or sponge to practice grip timing
- “Receive → hold 3 seconds → return” sequences
- Alternating left/right hand receive (person side), arm always same side
- Rhythm tasks: place object every N seconds on a beep / screen cue

### Fine motor / cognition lite
- Place puzzle pieces on a board (large pieces only)
- Match lids to containers
- Thread large beads onto a thick rod (arm holds rod or bead)
- Flip large cards one by one into a box
- Sequence tasks: “bring red, then blue, then green”

### Sit-to-stand adjacent (object support only)
- Bring a target object the person must stand briefly to take (caregiver present)
- Place a reward object slightly farther after successful reps
- Hold a light resistance band handle in a fixed pose for the person to pull (if grip + safety rated)

### Tracking / feedback hooks (software side)
- Count successful pick-and-place reps
- Log session duration and rest breaks
- Show simple progress (“5/10 cups placed”)
- Stop / pause on caregiver button or voice “stop”

---

## 4. Safety constraints

### Never / hard limits
- Never lift, support, or move a person
- Never handle open hot drinks, knives, chemicals, or open meds
- Never grip hearing aids, glasses lenses face-on, or fragile medical devices roughly
- Never operate unsupervised near pets / children without locks
- Never exceed payload / reach / speed limits of MakerMods arm
- Never block emergency exits or walker paths with the base

### Soft grip & object rules
- Prefer soft / compliant gripper pads
- Whitelist objects by size, weight, and shape
- Detect slip / failed grasp → retry once → stop and alert
- Prefer closed containers for liquids
- Avoid glass unless padded cradle / dedicated holder exists

### Workspace & motion
- Height ceiling: no unexpected overhead swings toward the head
- Slow default speed; “turtle mode” near people
- Keep a keep-out zone around the person’s torso / face
- Emergency stop: physical button + software stop + caregiver tablet
- Homing pose when idle (arm folded, clear of walkways)
- Base fixed / marked so it cannot tip or roll into the person

### Human factors
- Caregiver must be able to override every action
- Clear audio or light cues before motion starts (“moving…”)
- Confirm high-risk actions (“Bring water?” → yes)
- Log who requested the action (person vs caregiver)
- Fail safe: power loss → brakes / limp to safe pose if available

### Privacy & dignity
- No continuous bedroom camera streaming by default
- Local processing preferred for vision
- Easy mute / cover for any camera
- Person can refuse assist without friction

---

## 5. MVP demos (showable fast)

Pick a few that fit one table + chair setup.

### Demo A — “Bring me that”
1. Person sits at table  
2. Says / presses “bring remote”  
3. Arm picks remote from known spot → hands to person  
4. Person returns remote → arm parks it on charger / home spot  

### Demo B — “I dropped it”
1. Soft object on floor in marked zone  
2. Arm picks it up → places on table  
3. Show “no bending” story for elderly care  

### Demo C — “Morning tray”
1. Arm assembles a small tray: water bottle, glasses case, phone  
2. Slides / places tray in front of person  
3. Optional: return empties to a bin  

### Demo D — “Rehab 10 reps”
1. Soft block pick-and-place left ↔ right  
2. On-screen counter 1…10  
3. Pause / stop button for caregiver  
4. Show session summary  

### Demo E — “Night help”
1. Dim light scene  
2. Person presses bedside button  
3. Arm brings water bottle + phone from side table  
4. Returns to folded safe pose  

### Demo F — Training vs real arm
1. Same task on training arm (sim / lighter unit)  
2. Replay on actuated arm  
3. Pitch: safe practice → real assist  

---

## Extra idea bank (quick hits)

- Voice: “arm, bring water”
- Big-button UI for arthritic hands
- Preset scenes: Morning / TV time / Bedtime / Rehab
- Favorite-object memory (named positions)
- Multi-room later: mobile base — out of MVP
- Integration with nurse call / family chat notification when assist is used
- Soft toy / comfort object fetch for dementia-friendly calm routines
- Table wipe with a dry cloth pad (advanced; keep out of first MVP)
- Plant watering with measured closed bottle (advanced)
- Laundry assist: move a light clothing item from chair to basket

---

## Suggested first build order

### Software (this repo — already prioritized by impact)

1. Medicine schedule + Missed **caregiver alerts** (chronic-care continuity)  
2. **Adaptive** “encourage self-care first” before arm assist  
3. Companion **check-ins** + family memories + **video-call URL** bridge  
4. Soft **activity prompts** (voice only — not clinical therapy)  
5. Then hand off to arm hardware for physical assist  

### Arm team (hardware demos)

1. Fixed table workspace + emergency stop  
2. Object whitelist (3–5 items) + “bring X”  
3. Floor pickup of one soft object  
4. One rehab pick-and-place loop with counter  
5. Caregiver override + clear “moving” cue  

---

## Out of scope for early hack

- Cooking, stove, hot liquids  
- Toileting / bathing assist  
- Medication dosing / injection  
- Walking with / supporting the person  
- Stairs, outdoor, vehicle  
- Full apartment autonomous navigation  
