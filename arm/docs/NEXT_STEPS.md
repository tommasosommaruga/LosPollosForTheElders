# Improvements to prioritise

## Evidence from the SAM-only check

On 2026-11-15, the default `text_pick.py` command was run against the supplied
standing/hand photos for `white cup`. Cached SAM 3 weights were used with
Hugging Face offline mode and all Requests HTTP calls forbidden. The command
completed without a checker service or robot connection.

- Hand image: one selected cup mask.
- Standing image: AMBIGUOUS because both the target cup and a background cup
  passed the SAM score threshold. No pickup request was produced.
- Hand silhouette posture estimate: `flat`, although the cup is upright. Image
  aspect ratio changes with camera viewpoint and is not a reliable 3D posture.

This is one diagnostic example, not a measured recognition success rate. The
offline regressions include a default-command test that forbids
checker and controller HTTP calls. The earlier checker-enabled photo results
should not be treated as SAM-only benchmark results.

## Implemented follow-up

The integrated `text_pick.py` path now supports a camera-bound image search
polygon, mask quality checks and explicit point selection when multiple objects
remain. It reports reasons per candidate and abstains on uncertain targets.
It no longer infers posture from mask aspect ratio; measured item profiles define
the supported method and dimensions before live execution.

The same two photos were rerun with cached SAM 3, network/checker calls forbidden,
and a deliberately marked image search region. The intended standing-view cup
was uniquely selected (8,926 mask pixels); the background cups were excluded.
The hand view selected one cup (38,056 pixels). Both retained unknown posture and
produced side-grasp **previews** using an explicit method. No metric calibration
or robot request was used. That demo polygon is an image filter, not measured
reachability; photos and local demo files are not published.

Independent camera/table validation records now bind unused measurements to the
active calibration, with changed-geometry and target-coverage checks. The
controller exposes table coefficients/touches and bounds peak joint reference
speed for uneven waypoint spacing, with quintic start/stop timing. These changes
do not establish physical accuracy or remove internal path corners.

See [the setup procedure](WORKSPACE_AND_VALIDATION.md). The remaining priorities
below require measured geometry or actual robot testing; no new real calibration
has been supplied, and none has been invented.

## Recommended implementation order

1. **Remeasure geometry.** Correct joint offsets/fingertip geometry, table plane,
   camera intrinsics/distortion and fixed-camera alignment. Verify unused physical
   points across the pickup area. Choose a position tolerance from the actual
   gripper/contact clearance; a low fitting residual alone is insufficient.
2. **Validate the search region against actual reachability.** Image-space
   filtering is implemented, but a drawn polygon is not a collision map. Measure
   usable table bounds and approach clearance. Keep raised object masks intact.
3. **Use measured object and gripper geometry.** Start with a small set of known,
   rigid objects, their dimensions and suitable grasp methods. Do not infer
   upright/flat posture solely from image aspect ratio. Measure opening, finger
   thickness, fingertip offsets and grip contact. Some existing example objects
   are wider than the nominal usable gripper opening.
4. **Execute the planned orientation.** Add and validate wrist-roll-aware IK and
   approach clearance before enabling top grasps. Test on the actual calibrated
   arm model; a 2D jaw line is not a verified robot orientation.
5. **Use the hand camera for feedback.** After hand-eye calibration and exposure /
   joint timestamp alignment, add bounded final-approach corrections, target-loss
   stops and visual lift verification. Camera feeds alone are not a control loop.
6. **Measure results on the Linux robot.** Record missing/duplicate-object errors,
   position/angle/width error, latency, successful lifts and slips across varied
   positions and lighting. Use those results to tune thresholds and decide whether
   another vision model would actually help.

References: [SAM 3 concepts and masks](https://huggingface.co/docs/transformers/model_doc/sam3),
[OpenCV camera and hand-eye calibration](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html).
