---
name: setup-environment
description: Set up the Medicine Buddy laptop environment (venv, deps, agents.local, camera indices, dry-run). Use on first session, after git pull on the robot host, or when hardware/doctor checks fail.
---

# Setup environment

## Robot host PC (pull and run)

```bash
git pull
./scripts/bootstrap.sh
./scripts/run_robot_host.sh                  # terminal 1
./scripts/run_conversation_with_medicine.sh  # terminal 2
```

Requires pollen `reachy_mini_conversation_app` installed so
`reachy-mini-conversation-app` is on PATH (or set `REACHY_CONVERSATION_CMD`).

## Details

1. Ensure `agents.local.md` exists (bootstrap copies the example).
2. Venv lives in `apps/medicine_buddy/.venv`.
3. Conversation env is generated at `integrations/reachy_conversation/.env.generated`.
4. Verify API: `curl http://127.0.0.1:8787/health`
