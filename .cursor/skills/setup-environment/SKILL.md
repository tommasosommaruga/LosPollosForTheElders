---
name: setup-environment
description: Set up the Medicine Buddy laptop environment (venv, deps, agents.local, camera indices, dry-run). Use on first session, after git pull on the robot host, or when hardware/doctor checks fail.
---

# Setup environment

## Robot host PC (pull and run)

```bash
git pull
./scripts/bootstrap.sh       # venv + pip install -e ".[dev]" (includes Pollen's conversation app) + .env
./scripts/run_robot_host.sh  # = python -m medicine_buddy.main (one process: conversation + API + guard)
```

## Details

1. Ensure `.env` exists (bootstrap copies `.env.example`) and `REACHY_HOST` is the robot's IPv4 address.
2. Ensure `agents.local.md` exists (bootstrap copies the example).
3. Venv lives in `apps/medicine_buddy/.venv`.
4. Verify API: `curl http://127.0.0.1:8787/health`; pages `/` and `/family`.
5. On the robot instead: `./scripts/install_on_robot.sh`, then Reachy Mini Control → Applications.
