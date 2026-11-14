---
name: medicine-vision-ocr
description: Dual-camera OCR for labeled medicine boxes with confidence gating and empty slots. Use when changing tray labels, OCR threshold, or slot matching.
---

# Medicine vision OCR

## Output

`{slot_id, label, score}` — refuse arm motion if `score < ocr_min_confidence` or ambiguous.

## Rules

- Match against known medicine names from DB
- Empty slots: no text or label EMPTY
- Overhead primary; wrist assist optional

## Code

- `VisionPort` in `ports.py`
- `vision_ocr.py` (real stub), dry-run fake tray map
