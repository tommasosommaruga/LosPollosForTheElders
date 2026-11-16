# Local vision/controller integration

The text workflow and Linux handoff are documented in `LINUX_HANDOFF.md`.
`text_pick.py` now exposes both-camera detection, masks, pixel area, grasp previews
and optional single-attempt side pickup. The local controller incorporates
upstream `c8b9797754ff0dbd4f1074ce155c11d3d4378062`'s checkerboard helper and calibration
reload while retaining the protocol, observation and read-only changes below.
The helper's default output stays under `controller/calib/`, even when that folder
does not exist yet. Upstream measured calibration and park poses were not imported.

This integration was prepared without contacting Station, opening a camera,
issuing a servo command, or pushing to GitHub. `controller/UPSTREAM.json` records
the source repository and revision. The local controller is a modified copy, not
a submodule or a deployment to the arm laptop.

## What changed

`pick_v5.py` uses `arm_bridge.py` and the controller's hardware-free contract in
`controller/arm/vision_contract.py`. Both use the upstream **nominal** opening of
5.1 cm with 0.4 cm clearance (4.7 cm usable). Measure the real gripper before
deployment and change the shared contract in both deployed copies. Runtime
capability checks reject mismatched controller limits. Widths are transmitted
without display rounding; 4.74 cm must not become an accepted 4.7 cm request.

Side requests now include `method: side`, measured `jaws`, table `x/y`, closing
height `z`, and `above`, all in cm. The controller applies `above` to the side
approach and final lift rather than silently using a fixed 6-cm lift. Responses
include Boolean `holding`, executed `method`, and `closing_angle_applied: false`.
The vision bridge no longer infers grasp success from the phrase "holding it"
in a later log message. It checks fresh controller status before submitting and
does not retry motion requests on failure/timeouts.

The reported `holding` remains the upstream encoder-based contact heuristic; it
is not verified possession or slip detection. The inherited controller is not a
full obstacle/self-collision planner.

## Closing angles: explicitly unresolved

The controller's IK moves joints 1–6 and deliberately leaves wrist roll (7)
unchanged. Vision top-grasp plans retain their requested closing angle, but both
the bridge and server reject them with `ORIENTATION_UNSUPPORTED`. Dry-run images
and plans still work. Existing manual top grabs without an orientation request
continue to use the upstream default orientation.

This is intentional: forwarding an ignored angle would preserve the original
mismatch. Executing vision-selected top grasps needs a separate orientation-aware
IK/controller change, verified using the real URDF, joint calibration, reachability
and collision constraints. Side grasps use the existing controller approach from
the base toward the object; the drawn jaw lines remain a visual proposal, not
proof of an executable full 6D gripper pose.

## Versioned request contract

Example request for a future approved hardware session (not executed in testing):

```json
{"protocol_version":1,"x":-12.0,"y":24.0,"z":4.0,"above":15.0,"jaws":3.4,"method":"side"}
```

`GET /status` exposes `capabilities` and `read_only`. Unsupported protocol versions,
invalid/nonfinite values, unsupported angles, oversized objects, pending
calibration and stale/missing joint or gripper readings are refused before the
grab routine. Individual joint timestamps prevent a fresh reading from one motor
from concealing another motor's stale feedback. The client refuses older servers
rather than falling back to a request that drops the grasp type or width.

Legacy pixel requests on the integrated server are still accepted. The vision
client now sends table coordinates directly and requires the selected camera to
be identified as the fixed stand camera. Never reuse a moving-camera homography.

## Read-only two-camera preparation

The controller supports either two images from the Station feed, with explicit
`--stand-index`/`--hand-index`, or two direct camera devices through `--camera`
(stand) and `--hand-camera` (hand). Direct mode uses indices 0 and 1. Camera roles
must be checked physically; defaults are not evidence of correct wiring.

The new `--read-only` server option disables **all** POST actions, including hold,
release, park and calibration writes. It does not start a simulation: starting
the real server still requires the user's separately approved hardware session.
No server was started during this integration.

`GET /observation` returns JPEGs, camera IDs, resolution, sequence numbers, receive
times, and copies of measured encoder/FK data taken when each frame arrives.
`T_base_gripper` is the transform from URDF link **Gripper_Base_v1_1** to the robot
base, with translations in metres. It is not the fingertip frame. The camera
mount transform must use that same gripper link as its parent.

The FK exporter uses `angles_true()` from **raw encoder readings**, never commanded
goals. Its midpoint-based encoder offset mapping still needs physical validation.
Pure URDF parsing/FK can now be tested with an explicitly supplied URDF without
importing Station.

Receive timestamps are not exposure timestamps. Accordingly, exported packets
say `synchronized: false`, and the geometry preflight refuses them for moving-camera
triangulation. An acquisition adapter must provide exposure timestamps on a common
clock and associate/interpolate the measured gripper pose at hand-camera exposure.
No fake precision or synchronization is inferred from arrival timing.

`two_camera_setup.json` is an intentionally incomplete calibration template.
Populate measured intrinsics/distortion, exact camera IDs/resolutions,
`T_base_camera` for the stand camera and `T_gripper_camera` for the hand camera.
Only mark it validated after independent physical accuracy checks. The blank
template cannot produce 3D results.

## Offline commands tested

Run from the vision repository root:

```sh
.venv/bin/python -B -m unittest -v test_vision test_integration

.venv/bin/python -B camera_capture.py \
  --from-json examples/synthetic_pair/observation.json \
  --out review_outputs/synthetic_capture

.venv/bin/python -B calibrated_pair.py \
  --observation examples/synthetic_pair/observation.json \
  --calibration examples/synthetic_pair/calibration.json \
  --matches examples/synthetic_pair/matches.json \
  --out review_outputs/synthetic_capture/points.json
```

The synthetic fixture uses a 10-cm camera baseline and a point one metre away.
It is solely a mathematical test: its white images, calibration, synchronized
flag and poses must **never** be substituted for physical calibration. All output
retains `motion_allowed: false`. Correspondence files are bound to frame sequence
numbers and calibration ID to prevent accidentally mixing captures. Points must
refer to the same physical feature, not the centers of two object masks.

`camera_capture.py --url ...` is a read-only GET collector for a later authorized
session. It was not used against the robot. Existing `two_view.py` can inspect
the saved `stand.jpg` and `hand.jpg`; calibrated geometry is kept separate from
Qwen's semantic opinion and tentative SIFT matches.

Saved-image pick testing uses `--image`, `--calib`, `--no-show` and **no --go**.
The tests also assert this branch cannot contact the controller. Older numbered
scripts and the user's existing `pick.jpg` are preserved.

## What remains before a hardware trial

- Install the local controller changes on the arm laptop only in an approved
  session. Keep that robot's own measured calibration/poses; the friend's example
  `calib/`, `poses/` and `moves/` were not installed here as active configuration.
- Supply the compatible NormaCore runtime, generated protobuf modules and the
  actual `hardware/elrobot/simulation/elrobot_follower.urdf`. No clean-machine
  Station installation or physical FK accuracy was validated here.
- Measure the jaw opening, encoder offsets and fingertip/camera mounting geometry.
- Calibrate and validate both cameras, align image/encoder timestamps, and verify
  positions on held-out measurements across the workspace.
- Implement/validate wrist-roll orientation control before executing vision top
  grasps. Validate approach clearance and feedback for real objects separately.

Source: https://github.com/ciloglu44/lospollos-arm/tree/5ab8b0602007dd6933db4de28e43532c7db8b587

## Offline validation results

- 48 unittest cases passed, covering selection/geometry regressions, request
  routing, width boundaries, side approach/lift behavior, structured results,
  read-only mode, stale feedback, and two-camera metadata/calibration checks.
- 30 Python files passed syntax parsing. The tracked diff passed whitespace checks.
- The synthetic two-camera replay reconstructed the expected point at (0,0,1) m.
- Cached SAM inference with no network/model downloads and no controller connection:
  `wide1.jpeg` / energy drink can returned AMBIGUOUS; `wrist1.jpeg` / energy drink
  can returned NO_GRASP. Outputs are in `review_outputs/integration_pick.jpg` and
  `review_outputs/integration_wrist_pick.jpg`.
- The supplied camera calibration still reports a 2.5-cm maximum fitting error
  and only five marks. The wrist-photo width estimate was about 12.4 cm, which
  also demonstrates why that calibration must not be assumed valid for another
  camera pose. No physical accuracy or successful physical grasp is claimed.

The recognition model was not changed in this integration, so the earlier
14-case SAM/Qwen evaluation was not repeated. The new tests target the changed
integration, grasp checks, and observation pipeline.
