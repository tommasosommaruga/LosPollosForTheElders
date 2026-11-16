# Robot vision: offline inspection and grasp planning

Historical development notes. The current deployment defaults to SAM 3 only;
Ollama/Qwen experiments below are optional past evaluations, not setup requirements.
Use the repository-root README and LINUX_HANDOFF.md for current instructions.

## Combined text-to-pick workflow

`text_pick.py` connects typed descriptions, SAM/Qwen selection, object masks and
pixel area, grasp proposals, both integrated camera feeds, and the arm bridge.
See [LINUX_HANDOFF.md](LINUX_HANDOFF.md) for installation, Station camera roles,
calibration, output files, current limitations and optional live pickup.

```sh
.venv/bin/python -B text_pick.py --image wide1.jpeg --hand-image wrist1.jpeg \
  --text "pick up the energy drink can" --category can --vlm ollama
.venv/bin/python -B -m unittest -q test_vision test_integration test_voice_pick test_text_pick
.venv/bin/python -B build_handoff.py
```

No calibration is needed for pixel previews. Motion needs a measured setup;
top-grasp angle control remains unsupported. `dist/elrobot-text-pick.zip` is the
source-only handoff for the Linux robot laptop.

The current pipeline uses SAM 3 masks and optional Qwen2.5-VL verification through
Ollama. `pick_v5.py` adds approximate table localization and parallel-jaw grasp
planning. Existing numbered scripts remain available. A local, modified copy of
the friend's controller is now included under `controller/`, pinned to upstream
revision `c8b9797754ff0dbd4f1074ce155c11d3d4378062`. See `INTEGRATION.md` for the
offline workflow, protocol changes, and deployment prerequisites.

## Run offline checks

```sh
.venv/bin/python -B -m unittest -v test_vision test_integration
.venv/bin/python -B eval_v2.py --tests tests_v2.csv --vlm ollama --out review_outputs/eval_v5_corrected_labels
```

The evaluator now uses selector v5 and requires the annotated point to be inside
the chosen mask. A missing point is UNVERIFIED, not PASS. `--selector v3` selects
the older recognition implementation; `eval_v1.py` remains unchanged for historical
evaluation behavior. Fourteen photographs/target combinations do not validate
physical grasp accuracy.

`tests_v1.csv` is preserved. `tests_v2.csv` moves only two foam-ring label points
from the holes onto visible foam, based on visual inspection: wrist1 (645,330)
to (600,360), wide1 (1170,530) to (1200,560). The stricter grader flagged both
original points even though the segmentation previews selected the right rings.

## Two-camera inspection

```sh
.venv/bin/python -B two_view.py \
  --stand '/path/to/stand.jpeg' --hand '/path/to/hand.jpeg' \
  --target 'white cup' --category cup --vlm ollama \
  --out review_outputs/two_view_cup
```

Use original unannotated images from the same stationary scene. This command has
no robot controller connection or motion flag. It writes:

- `stand.jpg`, `hand.jpg`, and `comparison.jpg`: segmentation/selection previews.
- `stand_mask.png` / `hand_mask.png`: masks when a unique target was accepted.
- `report.json`: independent decisions, model explanations, optional tentative
  feature correspondences, and a contextual multi-image Qwen review.

TARGET_IN_BOTH_VIEWS means a unique semantic match was found in each image. It
does not prove that both detections are the same physical object. NEEDS_REVIEW
preserves missing/ambiguous/failed checks. The joint review shows both scenes
with candidate boxes and detailed crops, but cannot override either decision.
This helps reveal crop-only mistakes without treating another model opinion as
ground truth. At most three SAM candidates per unselected view are reviewed.

SIFT matches are restricted to mask interiors and checked for mutual descriptor
agreement and epipolar consistency. Smooth, reflective, or heavily occluded items
may have too few reliable features. Even good matches do not establish metric
scale: this tool deliberately outputs no centimeters, object height, or executable
3D grasp. Mask centers and silhouette edges are not valid general stereo matches.

The separate `multiview_geometry.py` utility accepts measured camera intrinsics,
distortion and camera-to-base transforms, and triangulates supplied physical
point correspondences. It checks finite values, positive depth, reprojection
error and ray separation. Translations and output points are in **metres**. Its
hand-camera pose helper composes a forward-kinematics result with the measured
camera mounting transform; it does not compute robot kinematics from angles.
This module is tested with synthetic geometry, including rotated/distorted
cameras. It is not wired into the photo report or robot until real calibration
is supplied and validated. Its geometric gates do not authorize motion.

## Changes to existing behavior

- Selector v5 validates VLM types and confidence range. Malformed output produces
  CHECK_FAILED, including when another candidate appears positive.
- Ollama identity requests use a JSON schema; posture requests keep their own format.
- Duplicate detection requires mask overlap as well as box overlap.
- Top-grasp candidates are filtered using calibrated jaw-endpoint distance, with
  a final check after rounding. Side grasps skip empty slices and check actual
  slice width instead of a smoothed estimate.
- `pick_v5.py --image` requires `--calib` rather than silently reading a live camera.
- Motion in v5 is blocked with fewer than six calibration marks, a nonfinite or
  greater-than-1-cm validation error, or a target outside the marks' convex hull.
  Dry-run inspection remains available. The 1-cm limit is a provisional software
  gate, not a validated task-specific grasp tolerance.

The supplied camera.json has five marks and approximately 2.5 cm maximum fitting
error. It is inadequate for the new motion gate. Do not use a fixed table
homography on a moving wrist camera. Profile-based width scaling does not correct
the camera's pose. The integrated API now accepts method, physical jaw width,
table x/y, closing height and approach/lift height, and returns a structured
holding result. Requested closing angles remain unsupported and are explicitly
blocked instead of silently ignored. Older manual top-grasp requests without
an angle remain supported.

## Calibration plan for the fixed and hand cameras

The hand camera is rigidly mounted and joint angles are available. Required next
inputs are the arm kinematic model (URDF or link/joint transforms), joint angle
units and offsets, timestamps, and synchronized original camera frames.

1. Calibrate each camera's intrinsics and distortion using a measured ChArUco or
   checkerboard target at varied positions and orientations. Keep resolution,
   cropping and focus settings fixed. Validate using held-out images.
2. Establish the fixed camera's transform to the robot base using measured target
   points in the robot frame. A board-to-camera pose alone does not establish the
   board-to-robot relationship.
3. Record the stationary calibration board from the hand camera at diverse,
   stationary robot poses, including rotations about different axes. Record
   measured joint angles and convert them to gripper-to-base poses using forward
   kinematics. These captures require separately approved physical operation.
4. Solve hand-eye calibration for the camera-to-gripper mounting transform. With
   T_A_B defined as mapping coordinates in B into A:

   `T_base_handcamera(t) = T_base_gripper(q(t)) @ T_gripper_handcamera`

5. Validate on unused robot poses and known 3D points. Save pixel reprojection
   errors and physical position errors across the entire intended workspace.
6. Only then triangulate matched physical features using the current camera poses,
   rejecting points behind a camera, with weak viewing angles, or large
   reprojection error. Fit object geometry with uncertainty and transform grasps
   into robot coordinates. A static camera does not make the camera pair static.

Calibration records should include camera IDs, resolution, intrinsic matrix,
distortion model/coefficients, transform direction, length units, calibration
version, validation errors, and capture timestamps. Do not invent transforms from
these two photos or nominal object dimensions.

## Practical accuracy improvements

- Mount the standing camera rigidly and keep the working area large in its image.
  A tabletop workspace polygon can exclude background objects once calibrated;
  masking a tabletop alone must not discard tall objects above that plane.
- Capture synchronized frames and measured joint angles. Begin with stationary
  captures before evaluating latency during motion.
- Use diffuse lighting and original camera frames rather than recompressed chat
  images. Avoid focus/zoom changes after calibration.
- Treat cups with liquid, deformable packaging, and reflective bottles as separate
  grasp cases. A cup's 2D outline does not establish fill level, friction, wall
  strength, or gripping force. Jaw padding, force/current feedback and slip checks
  matter alongside perception.
- Use the fixed view for scene tracking and approach context, and the hand view
  for final alignment. Track observations between slower SAM/Qwen updates and
  reject stale poses. Neither current script implements visual servo control.
- Evaluate position error, angle error, width error, and repeatability on held-out
  objects/poses before physical grasp trials. Then measure grasp success and slip;
  recognition PASS alone is not a grasp result.

## Environment

The existing `.venv` contains the runtime dependencies and cached SAM 3 weights.
`requirements.txt` records package versions found during this review; it is a
snapshot, not a tested clean-machine lockfile. Ollama is a separate service and
requires `qwen2.5vl:7b`. The Python `ollama` package is not required because the
scripts use HTTP via requests. Hugging Face access/download setup is required
when the SAM weights are not already cached. Tests use standard-library unittest.

References:
- https://docs.opencv.org/5.0/main_modules/calib.html
- https://docs.ollama.com/capabilities/structured-outputs
- https://huggingface.co/docs/transformers/en/model_doc/sam3
