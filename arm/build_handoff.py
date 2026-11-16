"""Build a source-only Linux handoff; never include secrets or robot calibration."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
ROOT_FILES = """
README.md INTEGRATION.md LINUX_HANDOFF.md VOICE_DEMO.md
CALIBRATION.md NOTICE.md docs/DEVELOPMENT_NOTES.md .gitignore
requirements.txt requirements-voice.txt requirements-test.txt requirements-checkers.txt
docs/OPTIONAL_CHECKERS.md docs/NEXT_STEPS.md
calibration_report.py test_calibration_report.py
selection_policy.py configure_workspace.py calibration_validation.py test_reliability.py
docs/WORKSPACE_AND_VALIDATION.md
text_pick.py arm_bridge.py target_selector_v5.py target_selector_v3.py grasp_planner_v3.py
pick_v5.py voice_pick.py voice_items.json items_v1.json
two_view.py multiview_geometry.py calibrated_pair.py camera_capture.py two_camera_setup.json
eval_v2.py tests_v2.csv test_text_pick.py test_vision.py test_integration.py test_voice_pick.py
build_handoff.py
""".split()
CONTROLLER_FILES = """
controller/README.md controller/UPSTREAM.json controller/.env.example controller/arm_client.py
controller/arm/__init__.py controller/arm/arm.py controller/arm/click.py
controller/arm/kinematics.py controller/arm/observations.py controller/arm/reach.py
controller/arm/trajectory_timing.py
controller/arm/station.py controller/arm/vision_contract.py controller/vision/calibrate_board.py
""".split()
FIXTURES = [f"examples/synthetic_pair/{name}.json" for name in ("observation", "calibration", "matches")]


def main():
    files = sorted(ROOT_FILES + CONTROLLER_FILES + FIXTURES)
    payloads = {name: (ROOT / name).read_bytes() for name in files}
    manifest = {"format": 1, "upstream": json.loads((ROOT / "controller/UPSTREAM.json").read_text()),
                "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
                "note": "Source only. Synthetic fixtures are mathematical tests, never robot calibration."}
    destination = ROOT / "dist" / "elrobot-text-pick.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            archive.writestr("elrobot-text-pick/" + name, data)
        archive.writestr("elrobot-text-pick/MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    print(f"Built {destination} ({len(payloads)} source/fixture files)")


if __name__ == "__main__":
    main()
