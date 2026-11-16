# Calibration: what is available and what is accurate

Three different calibrations are involved. A segmentation mask does not establish
metric scale, height, or robot coordinates.

## 1. The robot's table plane

`controller/calib/table.json` stores `z = a*x + b*y + c`, in the robot base frame
with coordinates in metres, plus measured fingertip touches (`touches_m`).
`arm.reach.Table` loads it and uses it for heights above the table and movement
limits. This is directly accessible as a local JSON file on the robot laptop and
as `table_calibration` in the updated controller's read-only `GET /status`.

On the real robot, preserve its existing validated file, or teach it using
`python -m arm.reach table` from `controller/`, following the leader/mirroring
instructions. The command collects physical touches; it is not an offline test.
Plane fit residuals measure agreement among those touches. They cannot reveal a
shared error in forward kinematics, joint offsets or fingertip geometry.

In the pinned upstream table file, five touches have **9.40 mm RMS** and
**16.03 mm maximum** perpendicular residual to the stored plane. That is another
reason to remeasure this setup. It is not a measured error for the robot as it
is currently assembled, nor independent evidence of fingertip accuracy.

## 2. Fixed standing camera to table

`controller/calib/camera.json` contains camera index, image width and pairs
`[u_px, v_px, x_cm, y_cm]`. The controller exposes those pairs through `GET /status`.
The vision code uses the same homography mapping from table-level pixels to
table coordinates. `calibration_report.py --arm-url http://localhost:8890` audits
the active camera pairs using read-only HTTP; `--camera` audits a local file.

Measurements checked on 2026-11-15:

| Source snapshot | Marks | Fit RMS | Maximum fit error | Maximum leave-one-out error |
| --- | ---: | ---: | ---: | ---: |
| Development workspace `camera.json` | 5 | 1.53 cm | 2.54 cm | Not available with this validation rule |
| lospollos-arm `c8b9797754ff0dbd4f1074ce155c11d3d4378062`, `calib/camera.json` | 162 | 1.64 cm | 4.33 cm | 4.34 cm |

These are calculations from stored files, **not independently measured physical
pickup errors**. Neither file passes the current software camera criterion of at
least 6 marks and no more than 1 cm maximum leave-one-out error. That criterion
is provisional and is not proof of grasp accuracy. More marks are useful only
when their coordinates and camera setup are consistent. The actual current
robot may have different calibration; run the audit against its files. The owner
confirmed the setup has not moved and no new calibration has been made. Reusing
those files therefore retains their known errors rather than resolving them.

The new [independent validation workflow](docs/WORKSPACE_AND_VALIDATION.md) creates
an empty record bound to the active geometry, camera ID and resolution. Populate
it with unused measured points; live pickup requires successful checks and target
coverage. It neither fabricates nor changes the saved calibration. The recorded
files above still fail the fitting-error gate even if a validation file is supplied.

The upstream checkerboard helper supplies many corners, but all corners from a
board pose share its manually measured robot alignment. Holding out nearby
corners does not independently validate that alignment. Check unused physical
points and different board positions across the whole intended pickup area.
Record mean, RMS, maximum and repeatability of measured landing error, in mm.
The tolerated error must be smaller than the available jaw/contact clearance.

The homography describes the table plane. Applying it to the visible centre of
a tall cup does not give the cup's correct 3D grasp point. Current side plans
estimate the footprint from the bottom of the mask plus a measured item width;
height comes from measured item data. Perspective, occlusion and pose can still
bias those estimates. Grasp previews are proposals, not force-closure proofs.

## 3. Camera mounted on the gripper

The hand camera moves when the joints move. Its image cannot use the standing
camera's homography or a single fixed mapping measured at another arm pose.
For metric use, measure both cameras' intrinsics/distortion, the fixed camera's
pose in the base frame, and the hand camera's mounting pose relative to the
gripper. Associate measured joint angles with each exposure:

```text
T_base_handcamera(t) = T_base_gripper(q(t)) @ T_gripper_handcamera
```

The repository has FK export, a calibration template and offline triangulation
utilities. The template is intentionally unfilled. Station observations currently
carry receive timestamps, not synchronized exposure timestamps. The geometry
preflight refuses those packets for moving-camera triangulation.

Both cameras can remain connected continuously, but the current text pipeline
uses independent semantic checks. It does not yet estimate calibrated stereo
depth or continuously correct the arm during approach. It requires the hand
camera to see a unique matching target before a live pickup request; if that
view is occluded or points elsewhere, the request is blocked.

For this particular two-camera configuration, accurate 3D pickup needs the
measured transforms, temporal alignment, real joint calibration, validated
gripper geometry and orientation control. The two example photos alone do not
contain those measurements.

Reference: [OpenCV camera calibration and hand-eye methods](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html).
