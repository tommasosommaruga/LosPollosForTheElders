"""Vision-side contract and HTTP client. Construction never connects to hardware."""
import requests
from controller.arm.vision_contract import (
    PROTOCOL_VERSION, GRIPPER_OPEN_CM, JAW_MARGIN_CM, normalize_grab,
)


def grasp_request(x, y, z, above, grasp):
    body = {"protocol_version": PROTOCOL_VERSION, "x": float(x), "y": float(y), "z": float(z),
            "above": float(above), "jaws": float(grasp["width_cm"]), "method": grasp["type"]}
    # A top-down plan has a requested roll angle. Do not silently discard it.
    if grasp["type"] == "top":
        body["closing_angle_table_deg"] = grasp["closing_angle_table_deg"]
    return body


def request_errors(body):
    try:
        normalize_grab(body)
    except (ValueError, KeyError) as exc:
        return [str(exc)]
    return []


class ArmBridge:
    def __init__(self, url="http://localhost:8890", session=None):
        self.url = url.rstrip("/")
        self.session = session or requests.Session()

    def get(self, path):
        response = self.session.get(self.url + path, timeout=5)
        response.raise_for_status()
        return response.json()

    def submit_grasp(self, body):
        # Validate before any I/O. This method is called only after explicit --go.
        normalize_grab(body)
        status = self.get("/status")
        caps = status.get("capabilities", {})
        if caps.get("protocol_version") != PROTOCOL_VERSION or not caps.get("structured_grab_result"):
            raise ValueError("Controller upgrade required; refusing legacy fallback")
        if caps.get("gripper_open_cm") != GRIPPER_OPEN_CM or caps.get("jaw_margin_cm") != JAW_MARGIN_CM:
            raise ValueError("Vision/controller gripper limits differ")
        if status.get("read_only") or status.get("busy") or status.get("pending"):
            raise ValueError("Controller is read-only, busy, or calibration is pending")
        response = self.session.post(self.url + "/api/grab", json=body, timeout=120)
        response.raise_for_status()
        result = response.json()
        if result.get("ok") is not True:
            raise ValueError(result.get("error", "Arm refused request"))
        if type(result.get("holding")) is not bool or result.get("method") != body["method"]:
            raise ValueError("Invalid controller grasp result; do not retry automatically")
        return result
