"""Shared, hardware-free vision/controller contract. All request distances are cm."""
import math

PROTOCOL_VERSION = 1
GRIPPER_OPEN_CM = 5.1  # Upstream nominal value; physically measure before deployment.
JAW_MARGIN_CM = 0.4
MAX_JOINT_AGE_S = 0.25


def finite_number(value, name):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def width_fits(width, opening=GRIPPER_OPEN_CM, margin=JAW_MARGIN_CM):
    width = finite_number(width, "jaws")
    opening = finite_number(opening, "opening")
    margin = finite_number(margin, "margin")
    if width <= 0 or margin < 0 or opening <= margin:
        raise ValueError("Invalid gripper dimensions")
    return width + margin <= opening + 1e-9


def capabilities():
    return {"protocol_version": PROTOCOL_VERSION, "controller_build": "descent-taper-v1", "methods": ["top", "side"],
            "units": "cm", "gripper_open_cm": GRIPPER_OPEN_CM,
            "jaw_margin_cm": JAW_MARGIN_CM, "closing_angle_control": False,
            "observation_endpoint": "/observation", "structured_grab_result": True}


def normalize_grab(body, locate=None):
    """Validate before side effects. Legacy pixel requests remain supported."""
    if not isinstance(body, dict):
        raise ValueError("Expected an object")
    if "x" in body or "y" in body:
        x, y = (finite_number(body[k], k) for k in ("x", "y"))
    elif locate is not None:
        x, y = locate(finite_number(body["u"], "u"), finite_number(body["v"], "v"))
    else:
        raise ValueError("Provide table x/y coordinates")
    method = body.get("method", "top")
    if method not in ("top", "side"):
        raise ValueError("method must be top or side")
    if body.get("closing_angle_table_deg") is not None:
        raise ValueError("ORIENTATION_UNSUPPORTED: controller cannot execute a requested closing angle")
    z = finite_number(body.get("z", body.get("height", 4.0)), "z")
    object_height = body.get("object_height")
    if object_height is not None:
        object_height = finite_number(object_height, "object_height")
        if object_height <= 0:
            raise ValueError("object_height must be positive")
    above = finite_number(body.get("above", max(12.0, object_height + 5) if object_height else 12.0), "above")
    if z < .5 or above <= z:
        raise ValueError("Closing height must be >=0.5 cm and approach/lift height must exceed it")
    jaws = body.get("jaws")
    if jaws is not None and not width_fits(jaws):
        raise ValueError(f"NO_GRASP: width exceeds {GRIPPER_OPEN_CM - JAW_MARGIN_CM:g} cm usable opening")
    if body.get("protocol_version") is not None and (type(body["protocol_version"]) is not int or body["protocol_version"] != PROTOCOL_VERSION):
        raise ValueError("Unsupported vision protocol version")
    if body.get("protocol_version") == PROTOCOL_VERSION and jaws is None:
        raise ValueError("Vision requests must include measured jaws width")
    return {"x": finite_number(x, "x"), "y": finite_number(y, "y"), "z": z,
            "above": above, "jaws": jaws, "method": method}
