# Current pickup: automatic small-object profiles and both cameras

Branch: `feel-grab-reachy`. Pickup build: **`fine-centering-v3`**.
Running controller build: **`descent-taper-v1`**.
Both standing and wrist guidance are enabled by default in `grab_text.py`.
Use `--no-hand` only for an explicit standing-only comparison.

## What changed

- `small white cylinder` selects the operator's measured 2 cm long, 1 cm diameter
  object. The same prompt works for standing and sideways poses. SAM retries
  alternate descriptions of that same cylinder and closer table crops when
  needed. Projected upright/sideways shape hypotheses estimate its XY centre;
  uncertain orientation is labelled explicitly. An accepted upright pose chooses
  a 1 cm grip from its known 2 cm height; sideways chooses a 0.5 cm grip from
  its known 1 cm diameter. This is a shape-model decision, not SAM reporting
  centimetres. An uncertain pose retains the labelled 0.5 cm fallback.
- Changing the height requires silhouette overlap of at least 0.70, an upright
  versus sideways score margin of at least 0.12, at least 80 mask pixels, a
  minimum mask extent of 6 pixels and no clipping at the frame edge. These are
  heuristic fit checks, not calibrated probabilities or physical validation.
  The fit must also pass the standing-aim agreement check before height selection.
- `small black cap` selects a low-object profile using the supplied 5–10 mm
  height range. It still closes at 0.5 cm, without a per-run measurement. This
  is explicitly printed and recorded as `cap_height_range_fallback`, not an
  automatic depth measurement. A cap
  exactly 5 mm high is at the current fingertip-clearance boundary: reliable
  physical engagement there is not established. We have not lowered the
  controller's clearance or added a search into the table.
- Both profiles use the existing camera/table geometry. This is not general
  depth sensing. Cylinder dimensions are known; the cap's 0.75 cm height hint
  is a range midpoint, not a camera measurement. A centre fit differing by
  more than 2 cm from the original standing aim is rejected.
  The wrist hover uses the pose-dependent cylinder height or the cap range hint.
  Reports keep the height decision and its source; unknown/range-only heights
  are not sent to the controller as single-valued object heights.
- Guarded descent preserves its initial servo reference, then tapers the previous
  pose's sag compensation to zero at the closing-height endpoint. It does not
  apply fresh sag-correction pulses just before closing. Existing settling and
  blocked-joint relief remain. Joint reference speed is bounded.
- Normal pickup retains its numeric closing height and needs a complete planned
  descent. It does not require table contact to close. The separate below-table
  touch/search tool may still return a partial path, now labelled as partial.
- Wrist selection excludes matches more than a quarter image-width from the
  expected object before comparing confidence. A second look crops around the
  expected object using the same text prompt. It no longer substitutes an
  arbitrary "small white object" when the requested object is missing.
- For these profiles, wrist corrections are limited to 0.5 cm per move and 2 cm
  total suggested displacement from the standing aim; hover searches stay within
  1 cm. Other objects retain the 2 cm step / 8 cm displacement limits.
  Bottle and accepted-upright cylinder use at most five alignment looks, with
  another observation after every correction. The cap and sideways/uncertain
  cylinder use at most eight looks and a tighter estimated 0.15 cm offset limit
  instead of 0.3 cm. They require two observations within that limit, differing
  by at most an estimated 0.1 cm. The confirmation observation does not command
  another movement. Repeated camera frame IDs cannot count as fresh confirmation;
  these are host-received frame IDs, not synchronized exposure timestamps.
  Lost or unconfirmed targets revert
  to the standing aim. Confirmed wrist alignment skips the inherited extra 1 cm
  outward offset. These profiles also skip that offset on standing-only/fallback
  aiming; other objects retain it. `--reach-out` is an optional explicit override.
- Wrist diagnostics record the standing aim, saved jaw target pixel, each
  observed object centre, estimated offset, frame IDs and outcome (aligned,
  lost target, excessive correction, unconfirmed, etc.). They are included in
  the controller request/report and `pick_views/wrist_alignment.json`. An
  accepted wrist aim is still a hover alignment. This change does not establish
  continuous tracking during descent, correct a wrongly calibrated jaw target,
  or prove that a partial/incorrect SAM mask represents the physical grasp centre.
- Each controller grab writes a JSON report under `config/local/grasp_runs`,
  including failures during motion. Reports contain the build, request, nominal
  heights, compensated motor-target heights, measured encoder/FK heights and
  shoulder compensation. Calibration files and motor gains are not changed.

## Evidence and verification

The supplied bottle report was from the older `feel-stop-v2` controller. Its
nominal endpoint was -2.50 cm; replaying logged motor goals gave +0.36 cm, and
its final measured model height was +0.84 cm. Reconstructing nominal IK identified
a -57-step shoulder offset, consistent with carried-over hover compensation.
This was not a truncated nominal path.

The updated descent was exercised with the supplied geometry and inferred old
compensation in three ideal-servo simulations. Requested heights of 1.5, 2.7
and 2.0 cm produced motor-target heights of 1.529, 2.696 and 1.990 cm. These are
software checks, **not measured physical accuracy**. Real load, calibration and
motor response still need a physical test.

The offline suite passes **159 tests**, covering descent reference continuity,
endpoint compensation, speed bounds, contact backoff, partial-path labelling,
default camera mode, distant wrist detections, local crop coordinates, stale
frames, target loss, correction limits, close/lift without contact, and report
persistence on motion errors. New checks cover cylinder poses at different
table positions, alternate prompts, background-only detection retries, tiny
wrist masks, profile correction limits, and the actual client-to-controller
request path for upright/sideways cylinder and cap. Tests check that fitting
happens before height selection, rejected geometry cannot raise the grip, and
the cap range midpoint is not presented as a measured object height. SAM outputs and hardware
feedback are mocked; no local robot or GPU inference was used. Synthetic shape
fits do not establish physical calibration accuracy or real-image detection recall.
Fine-centering regressions cover an offset the old threshold accepted, two-image
confirmation without extra motion, jitter, repeated frames, target loss and
the final observation limit. A client-to-controller test verifies the corrected
cap XY, retained 0.5 cm height and saved alignment evidence. Bottle and accepted
upright-cylinder tests retain their previous centering behaviour.

## Linux update and start

With the arm stationary, stop the old pickup program and controller. Keep
Station running. From the existing checkout:

```bash
cd "$HOME/Projects/Robotics Hackathon/LosPollosCV_Arm"
git switch feel-grab-reachy
git pull --ff-only
git log -1 --oneline
```

If Git reports a conflict, preserve local changes; do not reset the checkout.
Controller terminal:

```bash
cd "$HOME/Projects/Robotics Hackathon/LosPollosCV_Arm/controller"
python3 -m arm.click --camera /dev/video4 --hand-camera /dev/video6
```

Select follower **Self-controlled** in Station and **Hold arm** on the arm page
at `http://localhost:8890`. Do not press the camera-calibration Reset button.
Pickup terminal:

```bash
cd "$HOME/Projects/Robotics Hackathon/LosPollosCV_Arm"
.venv/bin/python -u -B grab_text.py --hand
```

Type `small white cylinder` at `What now?`, whether it is upright or sideways.
On another attempt type `small black cap`. No `--close` or `--object-height`
is needed. Startup should print `Pickup build: fine-centering-v3` and
`standing camera + wrist alignment ON`; each request should print
`Controller build: descent-taper-v1`, `Automatic profile: ...; closing at ... cm`
and `Height decision: ...` explaining the chosen height or fallback.
A confirmed wrist correction prints
`wrist alignment confirmed; no extra outward offset`. If wrist detection is
unavailable or uncertain, the terminal explicitly reports standing-camera fallback.
Pulling does not update running Python processes.
This update changes the pickup client and diagnostics only. If the controller
already reports `descent-taper-v1`, restarting the pickup client is sufficient.

## First physical test and remaining calibration limit

Use one isolated known object in a small central pickup patch. The supplied
plane disagreed with one touch near the bottle by 1.6 cm; software cannot turn
that into a correct physical measurement. That uncertainty is larger than these
objects' grip clearance. Keep both camera mounts fixed and watch the first
attempt. Stop if the fingers press the table. Try the cylinder upright, then
sideways, then the cap using the same interactive program. Several consecutive
successes in one patch are needed before expanding the pickup area.

If physical height still disagrees, the existing optional local Z-plane repair
procedure is below. It is not part of the automatic object-selection command.

With controller/pickup stopped, Station running, and the follower controlled by
the leader in mirroring mode, back up and re-teach the local Z plane:

```bash
cd "$HOME/Projects/Robotics Hackathon/LosPollosCV_Arm/controller"
cp calib/table.json "calib/table.backup-$(date +%Y%m%d-%H%M%S).json"
python3 -m arm.reach table
```

Follow the four prompts: closed fingertips pointing down, barely touching the
bare table at four corners around the intended pickup area. Do not press down.
Then check an additional point in the middle, not one of the fitted corners:

```bash
python3 -m arm.reach where
```

With a light touch there, the displayed height should be near zero. Aim for
agreement within roughly 0.3 cm for the 2 cm test cylinder; this is a practical
test target, not a software guarantee. If readings differ by a centimetre,
investigate the touch/pose/calibration before another automatic pick. Exit
`where` with Ctrl+C, then restart the controller, select Self-controlled and
Hold arm. This teaches the Z plane; it does not recalibrate camera XY mapping.

## Other objects and optional overrides

Objects without one of these named profiles retain the older policy: half the
RGB-estimated upright height, clamped to 1.5–6 cm; unknown height uses 1.5 cm.
It is not a reliable automatic height estimator for arbitrary sideways or thin
objects. The terminal states this limitation. `--object-height CM` remains an
optional measured-height override and
must exceed 0.5 cm. `--close CM` overrides the fingertip closing height, not the
jaw width. Do not use either override for the automatic cylinder/cap test.

Seek several consecutive successes in the same verified patch before expanding
positions or changing objects. Always-correct pickup of arbitrary objects is
not established: wrist alignment happens above the object, not continuously
during descent, and this implementation does not derive calibrated depth from
the two RGB cameras. Broader reliability needs measured 3D geometry, identity
tracking and physical validation across object sizes and poses.

The existing wrist calibration contains a target pixel and a 2D pixels-per-cm
matrix. It does not supply a camera-to-gripper 3D transform or lens calibration.
The repository's `calibrated_pair.py` reconstruction utility is offline-only;
it is not wired into `grab_text.py`. To make cap and unknown-object heights
measurement-based, both cameras need measured intrinsics/distortion, the fixed
camera-to-base transform, the wrist camera-to-gripper transform and validated
matching of the same physical features across views. A checkerboard table-plane
fit alone does not supply all of these. See the
[camera calibration plan](DEVELOPMENT_NOTES.md#calibration-plan-for-the-fixed-and-hand-cameras)
and [OpenCV camera/hand-eye calibration](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html).
For the 5 mm cap, the measured plane and fingertip error must also be materially
smaller than its height; extra segmentation alone cannot resolve that limit.

## Send the result if it misses

Keep the controller running and, before parking or another attempt, run:

```bash
cd "$HOME/Projects/Robotics Hackathon/LosPollosCV_Arm"
.venv/bin/python -B collect_arm_debug.py
```

This performs read-only collection and creates a ZIP in Downloads, including
the original standing frame, selected segmentation mask, wrist images,
`wrist_alignment.json` and
recent controller reports. Transfer it
to your computer and attach it in the chat. It does not upload automatically.
