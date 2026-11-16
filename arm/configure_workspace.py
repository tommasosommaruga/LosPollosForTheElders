"""Mark the fixed camera's image search region. Does not calibrate or move the arm."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from selection_policy import workspace_polygon


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="original stand-camera frame")
    parser.add_argument("--points", nargs="+", help="ordered convex polygon vertices as x,y; omit to click")
    parser.add_argument("--camera-id", help="exact stand camera_id from GET /observation; required for live use")
    parser.add_argument("--out", required=True, help="workspace JSON destination")
    args = parser.parse_args(argv)
    image = Image.open(args.image).convert("RGB")
    view = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    points = []
    if args.points:
        points = [[float(v) for v in value.split(",")] for value in args.points]
    else:
        window = "Stand workspace: click corners; Enter saves; R resets; Esc cancels"
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(window, lambda event, x, y, *_: points.append([x, y]) if event == cv2.EVENT_LBUTTONDOWN else None)
        try:
            while True:
                preview = view.copy()
                if points:
                    cv2.polylines(preview, [np.asarray(points, np.int32)], len(points) > 2, (0, 220, 255), 3)
                cv2.imshow(window, preview)
                key = cv2.waitKey(30) & 255
                if key == 27:
                    return
                if key in (10, 13):
                    break
                if key in (ord("r"), ord("R")):
                    points.clear()
        finally:
            cv2.destroyAllWindows()
    config = {"schema_version": 1, "role": "stand", "camera_id": args.camera_id,
              "image_size": list(image.size), "polygon_px": points,
              "purpose": "image_search_region_only", "reachability_validated": False}
    polygon = workspace_polygon(config, image.size)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2) + "\n")
    cv2.polylines(view, [np.rint(polygon).astype(np.int32)], True, (0, 220, 255), 3)
    cv2.imwrite(str(output.with_suffix(".jpg")), view)
    print(f"Saved {output}; image search region only, not measured reachability or calibration.")


if __name__ == "__main__":
    main()
