# Arm / camera handoff — structured HTTP signals

Medicine Buddy (Reachy) asks the person to confirm, then sends **standardized JSON text** to the arm PC. The arm does **one-shot** detection + motion, then optionally a **one-shot** camera check that they took the medicine.

## Config on Medicine Buddy

```bash
MEDICINE_BUDDY_ARM_URL=http://<arm-pc-ip>:9090
# MEDICINE_BUDDY_ARM_TIMEOUT_S=90
```

If `ARM_URL` is empty, Buddy keeps `FakeArm` (demo).

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
- [ ] E-stop respected on arm side  

## Do not block on

Opening packaging, pill counting, or OAuth calendars.
