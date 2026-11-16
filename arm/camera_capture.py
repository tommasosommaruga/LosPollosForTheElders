"""Save read-only observation packets or replay them offline. Never sends POST."""
import argparse
import base64
import io
import json
from pathlib import Path

import requests
from PIL import Image


def save_packet(packet, output):
    if packet.get("schema_version") != 1 or not isinstance(packet.get("frames"), dict):
        raise ValueError("Invalid observation schema")
    images = {}
    for role, frame in packet["frames"].items():
        if role not in ("stand", "hand"):
            raise ValueError("Unexpected camera role")
        data = base64.b64decode(frame["jpeg_base64"], validate=True)
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            if list(img.size) != frame["image_size"] or img.format != "JPEG":
                raise ValueError("Frame format/resolution differs from metadata")
        images[role] = data
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    for role, data in images.items():
        (root / f"{role}.jpg").write_bytes(data)
    (root / "observation.json").write_text(json.dumps(packet, indent=2, allow_nan=False) + "\n")
    return {"roles": sorted(images), "synchronized": packet.get("synchronized") is True,
            "motion_allowed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-json", help="offline replay; no network")
    source.add_argument("--url", help="explicit read-only controller URL; GET /observation only")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.from_json:
        packet = json.loads(Path(args.from_json).read_text())
    else:
        response = requests.get(args.url.rstrip("/") + "/observation", timeout=10)
        response.raise_for_status()
        packet = response.json()
    print(json.dumps(save_packet(packet, args.out), indent=2))


if __name__ == "__main__":
    main()
