"""Connection to NormaCore Station.

Station's Python client lives inside the norma-core repo. This module finds
that repo and puts it on the import path, so the rest of our code can just do:

    from arm.station import new_station_client, send_commands, st3215

It looks for norma-core in this order:
  1. the NORMA_CORE environment variable (or NORMA_CORE= in .env)
  2. third_party/norma-core inside this repo (a clone or a symlink)
  3. a norma-core folder next to this repo
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Per-laptop settings come from .env (copy .env.example), so each laptop can differ.
_env_file = REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _value = _line.split("=", 1)
            if _value.strip():
                os.environ.setdefault(_key.strip(), _value.strip())

_candidates = [
    Path(os.environ["NORMA_CORE"]).expanduser() if os.environ.get("NORMA_CORE") else None,
    REPO_ROOT / "third_party" / "norma-core",
    REPO_ROOT.parent / "norma-core",
]
NORMA_CORE = next((p for p in _candidates if p and (p / "software" / "station").exists()), None)

if NORMA_CORE is None:
    raise RuntimeError(
        "norma-core not found. Either clone it:\n"
        "  git clone --depth 1 https://github.com/norma-core/norma-core.git third_party/norma-core\n"
        "or link the copy you already have:\n"
        "  ln -s /path/to/norma-core third_party/norma-core"
    )

sys.path.insert(0, str(NORMA_CORE))

from software.station.shared.station_py import new_station_client, send_commands  # noqa: E402
from target.gen_python.protobuf.drivers.st3215 import st3215  # noqa: E402
from target.gen_python.protobuf.station import commands, drivers  # noqa: E402

STATION_HOST = os.environ.get("STATION_HOST") or "localhost"
FOLLOWER_BUS = os.environ.get("FOLLOWER_BUS") or "auto"
LEADER_BUS = os.environ.get("LEADER_BUS") or "auto"

__all__ = [
    "REPO_ROOT",
    "new_station_client",
    "send_commands",
    "st3215",
    "commands",
    "drivers",
    "STATION_HOST",
    "FOLLOWER_BUS",
    "LEADER_BUS",
]
