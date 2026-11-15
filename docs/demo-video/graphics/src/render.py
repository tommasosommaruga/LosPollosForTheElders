"""Render every graphic in graphics.html to PNG with headless Edge or Chrome.

Usage (from anywhere):
    python docs/demo-video/graphics/src/render.py            # all, 16:9 and 9:16
    python docs/demo-video/graphics/src/render.py card-03    # only ids containing "card-03"

Output: docs/demo-video/graphics/out/16x9/<id>.png (1920x1080)
        docs/demo-video/graphics/out/9x16/<id>.png (1080x1920)
Overlays (labels, name tags, stamps) have transparent backgrounds; cards are full-frame.
Fonts (Source Serif 4, Inter) load from Google Fonts, so render while online.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "graphics.html"
OUT = HERE.parent / "out"
FORMATS = {"16x9": (1920, 1080), "9x16": (1080, 1920)}

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_browser() -> str:
    env = os.environ.get("BROWSER_BIN")
    if env:
        return env
    for c in BROWSER_CANDIDATES:
        if Path(c).exists():
            return c
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    sys.exit("No Edge/Chrome found. Set BROWSER_BIN to a Chromium-based browser.")


def graphic_ids() -> list[str]:
    return re.findall(r'\{\s*id:\s*"([^"]+)"', TEMPLATE.read_text(encoding="utf-8"))


def render(browser: str, gid: str, fmt: str, profile: str) -> Path:
    w, h = FORMATS[fmt]
    out = OUT / fmt / f"{gid}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    url = TEMPLATE.as_uri() + f"?id={gid}&fmt={fmt}"
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--default-background-color=00000000",
        "--virtual-time-budget=6000",
        f"--user-data-dir={profile}",
        f"--window-size={w},{h}",
        f"--screenshot={out}",
        url,
    ]
    out.unlink(missing_ok=True)
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    # On Windows msedge.exe can return before its child process writes the file.
    deadline = time.monotonic() + 30
    last = -1
    while time.monotonic() < deadline:
        if out.exists():
            size = out.stat().st_size
            if size > 0 and size == last:
                return out
            last = size
        time.sleep(0.5)
    raise RuntimeError(f"browser produced no file for {gid} ({fmt})")


def main() -> None:
    browser = find_browser()
    wanted = sys.argv[1:]
    ids = [i for i in graphic_ids() if not wanted or any(w in i for w in wanted)]
    with tempfile.TemporaryDirectory() as profile:
        for gid in ids:
            for fmt in FORMATS:
                path = render(browser, gid, fmt, profile)
                print(f"ok  {path.relative_to(OUT.parent)}")
    print(f"{len(ids) * len(FORMATS)} PNGs in {OUT}")


if __name__ == "__main__":
    main()
