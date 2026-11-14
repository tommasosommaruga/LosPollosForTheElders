---
name: adherence-watch
description: Verify medicine was taken via Reachy camera with voice fallback; log taken or missed. Use when changing adherence heuristics or timeout behavior.
---

# Adherence watch

## Flow

1. After place verify OK → Watching
2. Cam heuristic / stub detects take → Taken
3. Timeout → ask “Did you take it?” → Taken or Missed

## Code

- `AdherencePort` in `ports.py`
- `adherence.py`, dry-run console prompt
