# Set up target selection and validate the real geometry

Use `text_pick.py` for the integrated workflow. Earlier standalone selection and
pickup scripts do not apply all of these gates. All commands below run from the
repository root. Start with the controller in `--read-only` mode as described in
[Linux setup](../LINUX_HANDOFF.md), after updating and restarting its code.

## 1. Define where the standing camera should search

Capture both continuously connected cameras without moving the arm:

```bash
mkdir -p config/local
.venv/bin/python -B camera_capture.py --url http://localhost:8890 \
  --out config/local/capture
.venv/bin/python -c 'import json; print(json.load(open("config/local/capture/observation.json"))["frames"]["stand"]["camera_id"])'
```

Copy the printed standing-camera ID into the next command. Use the original,
uncropped `stand.jpg`. Click the corners of a convex search polygon in clockwise
or counterclockwise order; Enter saves, R resets, Escape cancels:

```bash
.venv/bin/python -B configure_workspace.py \
  --image config/local/capture/stand.jpg \
  --camera-id "COPY_THE_STAND_CAMERA_ID_HERE" \
  --out config/local/workspace.json
```

For a Linux environment without a GUI, supply measured image coordinates using
`--points x1,y1 x2,y2 x3,y3 x4,y4`. Inspect the generated polygon preview. The
configuration binds the polygon to this camera ID and exact image resolution.
Recreate it if the camera moves, its stream is reordered, or cropping changes.

This is an **image search area**, not a reachability or collision map. Keep it
within the table area you intend to validate. Selection checks the bottom band
of each silhouette against this polygon while preserving the full mask above
it. That band is only an estimate of where an object meets the table; overhangs,
occlusion and tilted objects can defeat it.

Background objects wholly outside the region are excluded. Two eligible objects
inside remain `AMBIGUOUS`; the higher score does not automatically win. Use a more
specific description or `--pick-at U V` to select a point inside one mask of the
current standing image. A point cannot override a defective mask or overlapping
uncertain candidate. The hand camera still needs its own unique match.

The report includes each candidate's target score, rejection reasons, and mask
quality. It removes detached speckles only when one component contains at least
95% of the mask, preserving holes. Fragmented, tiny or cut-off target masks, a
partly overlapping workspace boundary, or an in-region SAM-only score just below
the acceptance threshold cause review. These thresholds are heuristics to test
on your objects, not measured detection probabilities. `NOT_FOUND` means no
eligible target in the searched area, not proof of absence from the scene.

## 2. Check calibration using unused physical measurements

The unchanged setup lets you reuse the saved calibration as a starting point.
It does not remove its existing fitting errors. See [the recorded diagnostics](../CALIBRATION.md).
The active table coefficients and original touch samples now appear alongside
camera pairs in the controller's read-only `GET /status` response.

Create an empty measurement record bound to the active geometry, standing-camera
ID and current resolution:

```bash
.venv/bin/python -B calibration_report.py --arm-url http://localhost:8890 \
  --write-validation-template config/local/validation.json
```

This refuses to overwrite an existing record. It does not invent measurements,
refit calibration, or move the arm. Fill the record on the real setup:

- `camera_checks`: at least six **unused** table locations spanning the intended
  pickup area. Each entry is `{"pixel": [u, v], "measured_xy_cm": [x, y]}`. Measure
  x/y independently in the robot's base frame; do not compute them using the same
  camera homography or copy fitted calibration marks. Pixels refer to the current
  full-resolution standing image. Use separate board positions/physical marks so
  the checks do not all inherit one erroneous board alignment.
- `table_checks_base_m`: at least four new, spatially separated table points
  `[x, y, z]` in **metres**, not the original fitting touches. Use the robot's
  established physical teaching procedure and confirm the fingertip geometry.
  If these are encoder/FK-derived touches, they still share any FK/joint bias;
  compare real fingertip landing positions with independent measurement too.

Keep `geometry_sha256`, camera ID and resolution from the generated template.
Recalibration requires a fresh record and fresh checks, not editing its hash.
Keep repeated-trial measurements separately to quantify repeatability.

```bash
.venv/bin/python -B calibration_report.py --arm-url http://localhost:8890 \
  --validation config/local/validation.json
```

`independent_validation.passed` requires maximum camera and table check errors
within the provisional **1 cm** bounds. Live selection also requires the grasp
footprint inside both measured check regions, at least six fitting camera marks,
and maximum leave-one-out camera error within 1 cm. Copied fitting points,
degenerate measurements and changed geometry are rejected. Use tighter physical
tolerances where the finger/contact clearance requires them; passing a 1 cm
software limit is not a promise that a small object can be grasped.

The record validates the supplied numbers, not their truth. Neither fitting nor
these checks certify the full arm, camera-to-gripper geometry, repeatability,
collision clearance, or actual grasp success. The checker explicitly reports
`physical_accuracy_certified: false`.

## 3. Measure an object and choose its grasp method

Create `config/local/items.json`, using the exact text target as the key. Measure
its `width_cm`, `height_cm`, and suitable `grasp_height_frac` (strictly between
0 and 1); set `grasp` to `side` for the currently supported pickup route. Optional
`posture` describes the configured pose, not a vision measurement. Add
`"measurements_verified": true` only after checking those measurements and that
grasp method on the actual setup. The shipped example profiles are unverified.

The planner no longer guesses upright/flat posture from image aspect ratio.
An unknown object without a configured method returns `NEEDS_GRASP_METHOD` with
its selected mask and pixel area. `--grasp side` or `--grasp top` can generate an
unverified preview, but cannot turn example dimensions into measured geometry
or reuse verification for a different grasp method. Top execution remains blocked
until wrist-roll orientation control is implemented and validated. Also measure
the physical gripper opening; the existing nominal usable limit is 4.7 cm.

## 4. Review, then request one pickup

```bash
.venv/bin/python -B text_pick.py --arm-url http://localhost:8890 \
  --text "your measured object" --items config/local/items.json \
  --workspace config/local/workspace.json --validation config/local/validation.json
```

Inspect the masks, proposed contacts and `execution_blocks` in `result.json`.
For a physical trial, restart the controller without `--read-only` and add `--go`
to the same command. Both views must be fresh and advance again before submission;
a changed scene, calibration or camera blocks that request. No automatic retry
occurs after a lost motion response. Neither this process nor SAM performs
continuous obstacle avoidance or verifies possession after lifting.

The controller now bounds the peak speed of its continuous joint reference even
for unevenly spaced waypoints. Quintic timing gives zero speed and acceleration
at the start and end of a move. The path remains piecewise linear between joint
waypoints: internal corners, discrete command rounding, servo tracking, load and
compliance still need hardware evaluation. This is not an acceleration/jerk
guarantee or a tested smooth-pickup claim.

All `config/local/` contents, photos and measurements are ignored by Git and
excluded from the source handoff. Keep a separate backup on the robot laptop.
