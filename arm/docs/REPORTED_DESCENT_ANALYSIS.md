# Analysis of the reported table-contact failure

Report: `20261116T014141.815484Z_873b54c7.json`, from `feel-stop-v2`.
SHA-256: `4eb4eccb388c49ad192f6882e3cc8f63becb1d495e322487165189b8f67051fe`.
The original report remains local; no images or full joint log are published here.

| Recorded quantity | Value |
| --- | ---: |
| Requested closing height | +1.50 cm |
| `feel_all_the_way` | true |
| Search floor that replaced the closing height | -2.50 cm |
| Lowest commanded height in the sampled trajectory | -2.496 cm |
| Final sampled encoder/FK height | -0.422 cm |
| Final reported height after settling | -0.79 cm |
| Feedback samples | 52 |
| Windows that passed the stationary test | 0 |
| Smallest window span after startup | 0.101087 cm |
| Required maximum span | 0.100000 cm |
| Final learned lag baseline | 1.616 cm |

All heights above refer to the saved plane and encoder/kinematic model. They do
not show physical penetration depth or independently measure the tabletop.

## Why the failed detector did not stop

Its contact decision required a nearly stationary height window. That condition
never passed; even the smallest eligible span was slightly above its limit.
The free-motion lag baseline also kept growing as the model height changed.
The operator's observation of table pressure did not translate into the specific
stationary evidence that the software demanded. The report contains no force
measurement and no labelled timestamp for the first physical contact.

Replaying the detector from commit `7cefa06` with the recorded samples reproduces
no accepted contact. The later `DESCENT_NO_CONTACT` check then prevented jaw
closure. This explains the new no-close failure; that detector/check has already
been removed by the rollback.

## What the friend's rule does on these samples

On the same recorded samples, the original recent-lag rule first signals a stop
at sample 31 (zero-based), **2.6469 s** into the descent:

- Commanded model height: **0.7166 cm**.
- Measured encoder/FK height: **1.2691 cm**.

This is an offline comparison of decision rules. The commands and subsequent
feedback would differ in a real run with the old controller, so it is not proof
of contact at that height or a predicted successful pickup.

## The targeted request correction

The client's planned grip height was never used as a descent limit because
`feel_all_the_way` replaced it with `None` inside the controller. With that flag
omitted, the existing numeric-height guarded-grab path keeps **+1.5 cm** as its
target for this request, then closes and lifts. The original contact rule remains
available to stop earlier. This avoids requesting the extra 4 cm of downward
search; it does not solve errors in the model's physical coordinates.

The terminal now labels the planned height and whether it is estimated or a
fallback. Wrist guidance stays off. No motor gains, contact thresholds or
calibration files are retuned in this change.
