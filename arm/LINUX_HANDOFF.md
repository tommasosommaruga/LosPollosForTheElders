# LosPollosCV_Arm: Linux setup

This folder combines Robot_Vision with the modified lospollos-arm controller at
upstream commit `c8b9797754ff0dbd4f1074ce155c11d3d4378062`. Run `text_pick.py` for the
new combined workflow. The controller stays in `controller/`.

Type an object description → SAM 3 masks and scores → unique
object selection → proposed jaw contacts and approach → optional HTTP pickup.

The standing camera supplies approximate table coordinates. The integrated hand
camera supplies an independent identity check and close-up preview. Matching
descriptions in both views do **not** establish stereo correspondence or depth.
There is no visual servoing, full scene collision planning, or verified 6D grasp.

## Install on the Linux laptop

Use Python 3.12 and the robot's existing working NormaCore checkout/Station runtime.
The vision dependencies below are a snapshot of the Mac environment. A clean Linux
installation and physical robot operation have not been tested here.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -B -m unittest discover -q
```

Install the Torch build suitable for the Linux machine's CPU/CUDA setup if the
snapshot is not compatible. SAM 3 needs access to `facebook/sam3` on Hugging Face,
including its access agreement, credentials for the first download and sufficient
memory. **No Ollama installation, Qwen model, API key or model server is needed.**
The default is `--vlm none`: SAM runs directly in Python, selection uses SAM scores,
and grasp method comes from a measured item profile or an explicit preview choice.
It does not infer 3D posture from silhouette aspect ratio. Optional checker adapters are described
separately in `docs/OPTIONAL_CHECKERS.md` and run only when explicitly selected.
The speech packages are not needed for text input.

Point `NORMA_CORE` to the existing checkout. It must include the generated Station
protobuf modules and `hardware/elrobot/simulation/elrobot_follower.urdf`.
Set `FOLLOWER_BUS` to the real follower's configured board ID if automatic selection
is ambiguous. `controller/.env.example` shows the variable names.

Also install the dependencies of that NormaCore checkout in the interpreter used
to run the bridge (follow its own setup instructions). The vision requirements do
not install Station, generated protobufs or device drivers. If Station runs in a
separate working environment, run `arm.click` with that environment's Python.

Preserve this physical robot's own measured `calib/table.json`, `calib/camera.json`
and `poses/park.json` under `controller/` if reusing a validated setup. No upstream
example calibration or poses are included in the bundle. Redo calibration if the
camera, table, mounting, resolution or joint calibration changed.

The bridge requires `controller/calib/table.json` even in read-only mode. If this
robot has no valid table file, teach it first on the robot laptop:

```bash
cd controller
export NORMA_CORE=/absolute/path/to/norma-core
../.venv/bin/python -m arm.reach table
```

Follow the command's leader/mirroring procedure to touch the table at measured
points. This is physical calibration, not a software smoke test. Return to the
repository root afterward. Do not copy a table file from a different robot/setup.

## Start with both integrated cameras from Station

With Station already configured for the robot and both cameras, start the bridge
in a separate terminal. This command connects to Station; run it on the robot
laptop during setup, not as an offline test.

```bash
cd controller
export NORMA_CORE=/absolute/path/to/norma-core
../.venv/bin/python -m arm.click --stand-index 0 --hand-index 1 --read-only
```

Open `http://localhost:8890` and check which physical camera each feed shows.
The indices are examples: set `--stand-index` and `--hand-index` to the observed
Station ordering. The camera selected on the page must be the fixed standing
camera. `--read-only` exposes status/observations and rejects all control and
calibration-write POST requests.

If Station does not export both images, use direct devices instead, with Station
configured to release those camera devices:

```bash
../.venv/bin/python -m arm.click \
  --camera /dev/video4 --hand-camera /dev/video6 --read-only
```

The device paths are examples, not autodetected assignments. Direct mode uses
index 0 for the stand camera and 1 for the hand camera. It needs ffmpeg with V4L2.

## Type a request

From the project root, with the above controller running:

```bash
.venv/bin/python -B text_pick.py \
  --arm-url http://localhost:8890 \
  --text "pick up the blue pill box" --category box --grasp top
```

Omit `--text` to get a typing prompt. There is no arm movement without `--go`.
The command reads both camera images from `/observation`, checks each feed's
receive age, and uses the controller's calibration. An uncalibrated camera still
supports pixel-only previews.

To test saved images on either laptop, no controller or calibration is needed:

```bash
.venv/bin/python -B text_pick.py \
  --image /path/to/stand.jpg --hand-image /path/to/hand.jpg \
  --text "pick up the energy drink can" --category can --vlm none
```

Use original images of the same stationary scene. Add `--calib /path/to/camera.json`
only for the matching fixed-camera setup to inspect metric proposals. Saved images
can never be combined with `--go`.

Each request creates a separate directory under `review_outputs/text_pick/`:

- `stand_preview.jpg`, `hand_preview.jpg`: masks and boxes; red cross = proposed
  grasp point; green contacts = proposed jaw closure. Orange means width does not fit.
- `stand_mask.png`, `hand_mask.png`: exact selected object regions, only when unique.
- `stand_input.jpg`, `hand_input.jpg`: frames used for the decision.
- `result.json`: object-presence decision, pixel area, bounding box, grasp and
  approach, calibration diagnostics, controller request and reasons blocking motion.

Pixel area measures the visible silhouette. It is not surface area in square
centimetres. Unknown objects get a mask and area; add `--grasp side` or `--grasp top`
for an unverified pixel proposal. Physical height and width must be measured for a
side pickup. `items_v1.json` holds exact-name item profiles; its existing dimensions
are unverified examples. Follow [workspace and validation setup](docs/WORKSPACE_AND_VALIDATION.md)
to define a search region, record independent calibration checks, and create the
measured item profile required for motion. Existing setup being unchanged does
not eliminate residual errors already present in its calibration.

## Calibrate the fixed camera

The upstream checkerboard helper is included in `controller/vision/`. It writes
`controller/calib/camera.json` by default and supports `/api/reload`. Calibration
requires the actual camera and measured corner positions in the robot's frame.
It can update calibration only when the controller runs without `--read-only`.

```bash
cd controller
../.venv/bin/python vision/calibrate_board.py --make-board
../.venv/bin/python vision/calibrate_board.py \
  --page http://localhost:8890 --square-cm 2.5
```

Replace `2.5` with the measured printed square size. Board consistency and
held-out corner error do not independently validate robot landing accuracy:
corners share the same manually measured board pose. Check separate physical
points across the intended workspace. Do not reuse a fixed homography for the
moving hand camera. See `INTEGRATION.md` and `two_camera_setup.json` for calibrated
two-camera geometry; that calibration template is intentionally incomplete.

## Physical pickup

The current executable route is a **side approach for a fitting object**, with
measured geometry and a validated stand calibration. The gripper's nominal usable
opening is 4.7 cm (5.1 cm opening minus 0.4 cm clearance); measure it on the robot.
The example energy-drink can profile is 5.3 cm wide and must not be treated as a
fitting object. Top proposals preserve the requested jaw angle, but execution is
blocked with `ORIENTATION_UNSUPPORTED`: upstream IK leaves wrist roll unchanged.

During a physical trial, start the controller without `--read-only`, select the
correct follower control mode, and use a measured item profile. Then, from the
project root, explicitly add `--go` to a live command:

```bash
.venv/bin/python -B text_pick.py \
  --arm-url http://localhost:8890 --text "your measured object" \
  --items config/local/items.json --workspace config/local/workspace.json \
  --validation config/local/validation.json --go
```

`--go` is the execution request; there is no second confirmation prompt. Missing,
ambiguous or failed detections, uncertain masks, missing/unverified dimensions,
an oversized object, missing workspace or independent validation, inadequate
calibration, unsupported orientation, a missing hand match, stale feeds, changed
camera/calibration or visible scene changes block submission. Before submitting,
both frames must advance and still resemble the planned scene. This pixel-change
check is a conservative gate, not a guarantee that the scene is unchanged.
Station timestamps are receive times, not exposure times.

The controller checks protocol compatibility, joint feedback and reachability.
It does not plan around arbitrary obstacles. No code in this handoff has been
physically validated on the arm. `HOLDING_REPORTED` means the controller's encoder
contact heuristic fired, not verified possession. A lost motion response produces
`MOTION_RESULT_UNKNOWN` and is never automatically retried.

## Share the code

```bash
.venv/bin/python -B build_handoff.py
```

Send `dist/elrobot-text-pick.zip` to your teammate, or put the extracted source
folder in a repository. The bundle uses an explicit source allowlist and includes
the offline tests and synthetic fixtures. It excludes credentials, local models,
virtual environments, photos, logs, robot calibration and recorded movements.

The upstream README mentions `arm.calib`, but the referenced upstream revision
does not contain that module. This handoff does not depend on that missing command.
