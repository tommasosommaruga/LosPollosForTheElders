# Arm / camera handoff — structured HTTP signals

Medicine Buddy (Reachy) asks the person to confirm, then sends **standardized JSON text** to the arm PC. The arm does **one-shot** detection + motion, then optionally a **one-shot** camera check that they took the medicine.

## Config on Medicine Buddy

```bash
MEDICINE_BUDDY_ARM_URL=http://<arm-pc-ip>:9090
# MEDICINE_BUDDY_ARM_TIMEOUT_S=90
```

If `ARM_URL` is empty, Buddy keeps `FakeArm` (demo).

## Quick start on the arm PC

```bash
python3 integrations/arm/arm_listener.py
# listens on 0.0.0.0:9090 — logs each JSON POST, returns stub results
```

**Minimum code to replace:** in `integrations/arm/arm_listener.py`, only change the bodies of:

1. `run_dispense(cmd)` — your OCR + pick-to-bowl + place verify  
2. `run_check_taken(cmd)` — one-shot camera taken/missed  
3. `run_move_object(cmd)` — non-medicine graspables  

Keep the JSON response keys the same. The HTTP server wrapper can stay as-is.

Buddy logs every outgoing POST (`arm POST /v1/dispense → {…}`) and Reachy speaks the `speak_hint` (sending request → placed / failed).

## Protocol (`schema`: `medicine_buddy.arm.v1`)

Code: `medicine_buddy/arm_protocol.py`, client: `medicine_buddy/http_arm.py`.

### 1. After person says yes → dispense

`POST {ARM_URL}/v1/dispense`  
`Content-Type: application/json`

```json
{
  "schema": "medicine_buddy.arm.v1",
  "message_type": "dispense",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "medicine": { "id": 12, "name": "Aspirin", "person": "Mario" },
  "slot_hint": null,
  "ocr_min_confidence": 0.65,
  "actions": ["locate", "pick_to_bowl", "verify_place"],
  "one_shot_detect": true,
  "dry_run": false
}
```

**Arm response (same HTTP request body):**

```json
{
  "schema": "medicine_buddy.arm.v1",
  "message_type": "dispense_result",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "ok": true,
  "final_state": "placed",
  "slot_id": 2,
  "label": "ASPIRIN",
  "score": 0.91,
  "place_verified": true,
  "detect_detail": "box_in_bowl",
  "error": null
}
```

`final_state`: `placed` | `failed` | `aborted`  
`ok: false` → Buddy speaks failure, dose state **Failed**, no Taken.

### 2. After place → one-shot “did they take it?”

`POST {ARM_URL}/v1/check_taken`

```json
{
  "schema": "medicine_buddy.arm.v1",
  "message_type": "check_taken",
  "request_id": "…",
  "medicine": { "id": 12, "name": "Aspirin", "person": "Mario" },
  "slot_id": 2,
  "timeout_seconds": 8.0,
  "one_shot_detect": true,
  "dry_run": false
}
```

**Response:**

```json
{
  "schema": "medicine_buddy.arm.v1",
  "message_type": "check_taken_result",
  "request_id": "…",
  "ok": true,
  "taken": true,
  "via": "camera",
  "detail": "hand_to_mouth_or_bowl_cleared",
  "error": null
}
```

`taken`: `true` | `false` | `null` (inconclusive → Buddy asks by voice).

### 3. Non-medicine graspable → move_object

After the person confirms (e.g. “bring the water bottle”), Buddy POSTs a generic move. **Arm owns** locate / pick / place; Buddy only sends structured text.

`POST {ARM_URL}/v1/move_object`

```json
{
  "schema": "medicine_buddy.arm.v1",
  "message_type": "move_object",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "object": { "label": "water bottle", "kind_hint": "bottle" },
  "destination": "bowl",
  "actions": ["locate", "pick", "place"],
  "one_shot_detect": true,
  "dry_run": false
}
```

`kind_hint` is optional (`bottle`, `box`, `cup`, …) — you may ignore it.  
Scheduled medicines still use `/v1/dispense`, not this path.

**Arm response:**

```json
{
  "schema": "medicine_buddy.arm.v1",
  "message_type": "move_object_result",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "ok": true,
  "final_state": "placed",
  "detect_detail": "bottle_in_bowl",
  "error": null
}
```

`final_state`: `placed` | `failed` | `aborted`  
`ok: false` → Buddy speaks failure; no dose pipeline.

## Flow (locked)

1. Reachy: “Should I bring your {med}?” → **only after yes**  
2. Buddy POSTs `/v1/dispense`  
3. Arm one-shot locate + pick closed box → bowl + place verify  
4. Buddy POSTs `/v1/check_taken` (unless conversation paused after place)  
5. Dose → **Taken** | **Missed** | **Failed**

## Demo success criteria

- [ ] No motion until Buddy sends `dispense` after voice yes  
- [ ] One-shot OCR/detect on arm; low confidence → `ok:false`  
- [ ] Closed box in bowl; place failure → `failed`  
- [ ] One-shot camera `check_taken` returns taken true/false/null  
- [ ] `move_object` after voice yes for bottles / other graspables (arm decides graspability)  
- [ ] E-stop respected on arm side  

## Do not block on

Opening packaging, pill counting, or OAuth calendars.
