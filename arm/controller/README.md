# lospollos-arm

Control of the ElRobot arm (NormaCore) for the Los Pollos elder-care demo:
go to a point, grab from the top or the side, place, park — driven by clicks on a
camera image or by object detection.

## How it fits together

```
object detection (vision/)  ──HTTP──►  arm page (arm/click.py, port 8890)  ──►  Station  ──USB──►  arm
        arm_client.py                     inverse kinematics + safety checks
```

- **Station** (NormaCore) owns the servos. Started once, left running.
- **arm/** talks to Station: joints, poses, inverse kinematics, the click-to-grab page.
- **arm_client.py** lets any program (e.g. the detection) send grab / place / park
  to the arm page. No extra libraries.

## On the laptop with the arm

Needs `norma-core` cloned next to this folder, and Station running.

```bash
# .env (not in git) — which USB board is which arm
STATION_HOST=localhost
FOLLOWER_BUS=5B61035132
LEADER_BUS=5B61034685
```

```bash
python3 -m arm.click --camera /dev/video4      # the arm page: http://localhost:8890
python3 -m arm.click --camera /dev/video4 --host 0.0.0.0   # also reachable by teammates
```

Follower dropdown in Station: **mirroring** to teach/calibrate, **(Self-controlled)** to run code.

## From any laptop (e.g. the detection)

```python
from arm_client import Arm
arm = Arm("http://<arm laptop IP>:8890")
arm.grab(x=-12, y=24, z=6.9, jaws=3.8, object_height=13.5)          # from the top
arm.grab(x=-12, y=24, z=6.9, jaws=3.8, method="side")               # from the side
arm.place(x=-6, y=27, z=6.9, object_height=13.5)
arm.park()
arm.open()
```

All in cm, in the arm's table coordinates (shown on the arm page):
`x, y` = where the object stands, `z` = height where the fingers close,
`jaws` = object width at the grip (refused if the gripper can't open around it),
`object_height` = sets the approach height.

## Files

| | |
|---|---|
| `arm/arm.py` | joints, poses, record/replay movements |
| `arm/kinematics.py` | arm model (forward kinematics), accuracy check |
| `arm/reach.py` | table, go-to-point, grab (top / side) with safety checks |
| `arm/click.py` | the web page: camera, calibration, grab, place, park, HTTP API |
| `arm_client.py` | send commands to the page from any program |
| `calib/` | table and camera calibration (redo if the camera moves) |
| `poses/`, `moves/` | saved poses (e.g. `park`) and recorded movements |
| `vision/` | object detection (SAM 3) |
