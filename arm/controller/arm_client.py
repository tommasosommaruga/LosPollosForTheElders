"""arm_client.py — call the arm from any Python program (e.g. the object detection).

The arm page (python -m arm.click) does all the motion and safety checks. This file
only sends it requests over HTTP. No extra libraries needed.

    from arm_client import Arm
    arm = Arm()                                   # or Arm("http://<laptop IP>:8890")
    arm.grab(x=-12, y=24, z=6.9, jaws=3.8, object_height=13.5)
    arm.place(x=-6, y=27, z=6.9, object_height=13.5)
    arm.park()
    arm.open()

All distances in cm, in the arm's table coordinates (the same x, y the page shows).
  x, y           table point the object stands on (middle of its base)
  z              height above the table where the fingers close (about the middle of the object)
  jaws           object width between the jaws: refused if the gripper can't open around it
  object_height  the object's height: the arm approaches and lifts 5 cm above its top

Every call waits until the arm has finished and returns the page's message.
It raises ArmError if the arm refuses (unreachable, too wide, already moving...).

From a terminal:
  python arm_client.py status
  python arm_client.py grab -12 24 --z 6.9 --jaws 3.8 --object-height 13.5
  python arm_client.py grab -12 24 --z 6.9 --jaws 3.8 --side
  python arm_client.py place -6 27 --z 6.9 --object-height 13.5
  python arm_client.py park
  python arm_client.py open
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:8890"
CLEARANCE_CM = 5.0


class ArmError(RuntimeError):
    pass


class Arm:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 120):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.URLError as e:
            raise ArmError(f"Can't reach the arm page at {self.url} ({e.reason}). Is `python -m arm.click` running?")

    def _post(self, action, body=None):
        body = {k: v for k, v in (body or {}).items() if v is not None}
        req = urllib.request.Request(self.url + "/api/" + action, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except urllib.error.URLError as e:
            raise ArmError(f"Can't reach the arm page at {self.url} ({e.reason}). Is `python -m arm.click` running?")
        if not out.get("ok"):
            raise ArmError(out.get("error", "the arm refused"))
        return self.status()["log"]

    def status(self) -> dict:
        """Calibration, busy flag, fingertip position, last message."""
        return self._get("/status")

    def grab(self, x, y, z, jaws=None, object_height=None, above=None, method="top") -> bool:
        """Pick up the object standing at x, y. method: "top" (come down from above)
        or "side" (gripper horizontal, slide in). Returns True if it's holding something."""
        msg = self._post("grab", {"x": x, "y": y, "z": z, "jaws": jaws, "method": method,
                                  "object_height": object_height, "above": above})
        return "holding it" in msg

    def place(self, x, y, z, object_height=None, above=None) -> str:
        """Carry what it holds to x, y, lower it so the fingers are at z, let go, lift."""
        if above is None:
            above = object_height + CLEARANCE_CM if object_height else 12.0
        return self._post("place", {"x": x, "y": y, "height": z, "above": above})

    def park(self) -> str:
        """Back to the saved park pose, keeping the grip."""
        return self._post("park")

    def open(self) -> str:
        """Open the gripper (let go)."""
        return self._post("release")


def main():
    p = argparse.ArgumentParser(description="Send commands to the arm page")
    p.add_argument("--url", default=DEFAULT_URL)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("park")
    sub.add_parser("open")
    for name in ("grab", "place"):
        c = sub.add_parser(name)
        c.add_argument("x", type=float)
        c.add_argument("y", type=float)
        c.add_argument("--z", type=float, required=True, help="closing / releasing height above the table, cm")
        c.add_argument("--object-height", type=float)
        c.add_argument("--above", type=float)
        if name == "grab":
            c.add_argument("--jaws", type=float, help="object width at the grip, cm")
            c.add_argument("--side", action="store_true", help="come in from the side instead of the top")
    a = p.parse_args()
    arm = Arm(a.url)
    try:
        if a.cmd == "status":
            st = arm.status()
            print(json.dumps({k: st.get(k) for k in ("log", "busy", "calibrated", "tip", "torque")}, indent=2))
        elif a.cmd == "grab":
            held = arm.grab(a.x, a.y, a.z, a.jaws, a.object_height, a.above, "side" if a.side else "top")
            print("Holding it." if held else "Closed, but nothing in the gripper.")
        elif a.cmd == "place":
            print(arm.place(a.x, a.y, a.z, a.object_height, a.above))
        elif a.cmd == "park":
            print(arm.park())
        elif a.cmd == "open":
            print(arm.open())
    except ArmError as e:
        sys.exit(f"Arm: {e}")


if __name__ == "__main__":
    main()
