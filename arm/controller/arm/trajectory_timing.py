"""Hardware-free timing for the existing piecewise linear joint path."""
import math

EASING_PEAK_RATE = 1.875  # max derivative of 10t^3 - 15t^4 + 6t^5 on [0,1]


def ease(fraction):
    t = max(0.0, min(1.0, fraction))
    return t*t*t*(10 + t*(-15 + 6*t))


def bounded_duration(waypoints, requested_seconds, max_speed):
    if len(waypoints) < 2 or not math.isfinite(requested_seconds) or requested_seconds <= 0:
        raise ValueError("Trajectory needs at least two points and a positive finite duration")
    if not math.isfinite(max_speed) or max_speed <= 0:
        raise ValueError("Trajectory speed limit must be positive and finite")
    keys = set(waypoints[0])
    if not keys or any(set(p) != keys or not all(math.isfinite(v) for v in p.values()) for p in waypoints):
        raise ValueError("Trajectory points must have identical joints and finite values")
    # Global easing traverses each segment in 1/n of its progress interval. The
    # largest segment slope, not total travel, determines the worst joint speed.
    n = len(waypoints) - 1
    largest = max(abs(b[m] - a[m]) for a, b in zip(waypoints, waypoints[1:]) for m in keys)
    return max(.2, requested_seconds, EASING_PEAK_RATE * n * largest / max_speed)
