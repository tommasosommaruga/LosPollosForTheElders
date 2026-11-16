"""Medicine Buddy -> arm: the most basic dispense. One request = locate, pick, put in the bowl, park.

  .venv/bin/python dispense_server.py                        # finds the bowl in the camera itself
  .venv/bin/python dispense_server.py --bowl -20 15          # or: bowl always at table x=-20, y=15 cm
  (Buddy's config: MEDICINE_BUDDY_ARM_URL=http://<this laptop's IP>:9090)

Needs the arm page running WITHOUT --read-only (python3 -m arm.click ... in controller/),
follower (Self-controlled), Hold arm pressed. Reading labels needs once: uv pip install easyocr

POST /v1/dispense   body: {"schema": "medicine_buddy.arm.v1", "message_type": "dispense",
                           "request_id": "...", "medicine": {"name": "Aspirin", ...},
                           "ocr_min_confidence": 0.65, "dry_run": false, ...}
     -> the dispense_result (response body of the same POST; it waits until the arm is done)
POST /v1/move_object  an everyday thing (Buddy's move_object tool):
                           {"message_type": "move_object", "object": {"label": "water bottle"},
                            "destination": "bowl", ...}   -> move_object_result
                      destination "bowl" (default) or "handover" (needs --handover X Y)
POST /v1/check_taken  always answers taken: null, so Buddy asks the person
GET  /health        is the server up, and which requests it takes
GET  /v1/last       the last request and result (for Family/debug)

  .venv/bin/python dispense_server.py --listen-only      # no arm, no camera: only show what
                                                         # Reachy sends (to test the connection)

Basic on purpose: one bowl position you give, no slots, place_verified is always false
(not checked yet), "actions" and "slot_hint" are accepted and ignored.
"""
import argparse
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import numpy as np
    import requests
    from PIL import Image

    import grab_text as G
    import target_selector_v5 as ts
except ImportError:   # fine for --listen-only, which needs no camera or arm packages
    np = requests = Image = G = ts = None

SCHEMA = "medicine_buddy.arm.v1"
LOCK = threading.Lock()          # one dispense at a time
LAST = {"request": None, "result": None}


def page(a, name, body=None):
    """Press one of the arm page's buttons; returns its answer (raises with the page's error)."""
    r = requests.post(a.arm_url + "/api/" + name, json=body or {}, timeout=180).json()
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or f"{name} failed")
    return r


def find_bowl(a, img):
    """Where the bowl is on the table (x, y cm), found by SAM 3 in the same picture. None if not seen."""
    ts.VLM_BACKEND = "none"
    res = ts.decide(img, a.bowl_text, None)
    if not res["candidates"]:
        return None
    best = max(res["candidates"], key=lambda c: c["target_score"])
    ys, xs = np.nonzero(np.asarray(best["mask"]))
    u, v = float(xs.mean()), float(ys.mean())          # middle of the bowl as the camera sees it
    xy = page(a, "click", {"u": u, "v": v, "size": img.width})
    return xy["x"], xy["y"], best["target_score"]


def load_meds(path):
    """meds.txt: one line per medicine, "Aspirin = small bottle" (Buddy's name = what it looks like)."""
    meds = {}
    if path:
        for line in open(path):
            if "=" in line and not line.strip().startswith("#"):
                name, look = line.split("=", 1)
                meds[name.strip().lower()] = look.strip()
    return meds


def match_med(meds, name):
    """What does this medicine look like, from --meds? Buddy's wording doesn't have to match exactly:
    "Aspirin bottle", "aspirin 500mg" and "ASPIRIN" all find the "Aspirin" line. Longest match wins."""
    if not meds:
        return None
    key = " ".join(name.lower().replace("-", " ").split())
    if key in meds:
        return meds[key]
    padded = " " + key + " "
    hits = [(len(k), v) for k, v in meds.items() if (" " + k + " ") in padded]
    return max(hits)[1] if hits else None


def locate(a, name, min_conf):
    """Find the medicine in the fixed camera. Returns (u, v, label, score, how).
    A name in --meds: look for what it looks like. Any other name: look for something that looks
    like the name itself; without --meds (or with --read-labels) first try reading it on a label."""
    img = Image.open(io.BytesIO(requests.get(a.arm_url + "/frame.jpg", timeout=10).content)).convert("RGB")
    look = match_med(a.meds, name)
    if look:
        ts.VLM_BACKEND = "none"
        res = ts.decide(img, look, None)
        if not res["candidates"]:
            return None
        best = max(res["candidates"], key=lambda c: c["target_score"])
        u, v = G.base_pixel(np.asarray(best["mask"]))
        return u, v, name.upper(), float(best["target_score"]), f"looks_like:{look}"
    hit = None
    if a.read_labels or not a.meds:   # reading labels is slow: skip it when a --meds list is used
        try:
            hit = G.find_word(img, name)
        except ImportError:
            hit = None
    if hit is not None and hit[3] >= min_conf:
        wu, wv, read, conf = hit
        obj = G.object_at(img, wu, wv, ["medicine", "medicine box", "blister pack", "bottle", "box", "object"])
        if obj is not None:
            u, v = G.base_pixel(obj["mask"])
            return u, v, read.upper(), float(conf), "label_read"
    ts.VLM_BACKEND = "none"
    res = ts.decide(img, name, None)
    if res["candidates"]:
        best = max(res["candidates"], key=lambda c: c["target_score"])
        u, v = G.base_pixel(np.asarray(best["mask"]))
        return u, v, name.upper(), float(best["target_score"]), "by_look"
    return None


NOT_FOR_THE_ARM = {"knife", "knives", "scissors", "blade", "razor", "syringe", "needle", "hot", "boiling",
                   "weapon", "gun", "person", "people"}


def locate_object(a, label):
    """Find an everyday thing in the fixed camera by its look. Returns (u, v, label, score, how)."""
    img = Image.open(io.BytesIO(requests.get(a.arm_url + "/frame.jpg", timeout=10).content)).convert("RGB")
    ts.VLM_BACKEND = "none"
    res = ts.decide(img, label, None)
    if not res["candidates"]:
        return None
    best = max(res["candidates"], key=lambda c: c["target_score"])
    u, v = G.base_pixel(np.asarray(best["mask"]))
    return u, v, label, float(best["target_score"]), "by_look"


# ---------- grab_text.py's pickup, used as it is (grab_text.py itself is not changed) ----------

PICK = {}     # what the current pickup did (one dispense at a time, under LOCK)


def grab_text_args(a):
    """The options grab_text.py's pickup reads, with its own defaults (both cameras on)."""
    return argparse.Namespace(text=None, word=None, arm_url=a.arm_url, close=a.close, object_height=None,
                              put_height=5.0, above=a.above, side=False, bring=None, no_hand=a.no_hand,
                              reach_out=None, no_feel=False, dry=False)


def _watch_grab_text():
    """Note what grab_text's pickup found and how the grab ended, without changing grab_text.py.
    Works with any version of grab_text.py: it watches go() (what was found) and the arm's
    answer to the grab request (whether the gripper is holding something)."""
    if getattr(G, "_watched_by_dispatcher", False):
        return
    go, real = G.go, G.requests

    def watched_go(g, img, u, v, what, score, *args, **kw):
        PICK.update(state="DRY", u=float(u), v=float(v), width=img.width, score=float(score))
        return go(g, img, u, v, what, score, *args, **kw)

    class WatchedRequests:
        def __getattr__(self, name):
            return getattr(real, name)

        def post(self, url, *args, **kw):
            r = real.post(url, *args, **kw)
            if url.rstrip("/").endswith("/api/grab"):
                try:
                    d = r.json()
                except ValueError:
                    d = {}
                rep = d.get("report") or {}
                if not d.get("ok"):
                    PICK.update(state="FAILED", detail=str(d.get("error") or "the arm refused the grab"))
                elif rep.get("slipped"):
                    PICK.update(state="SLIPPED", detail="lost it on the way up")
                elif d.get("holding"):
                    PICK.update(state="HOLD DETECTED", detail="the gripper is holding something")
                else:
                    PICK.update(state="MISSED", detail="the gripper closed on nothing")
            return r
    G.go, G.requests, G._watched_by_dispatcher = watched_go, WatchedRequests(), True


def pick_like_grab_text(a, text, dry):
    """Find and pick `text` exactly as grab_text.py does: SAM 3 with its object profiles, per-object
    closing height, wrist-camera alignment, guarded descent and the holding check."""
    _watch_grab_text()
    PICK.clear()
    PICK.update(state="NOT_FOUND", detail=f"could not find '{text}'")
    g = grab_text_args(a)
    g.dry = bool(dry)
    G.find_and_grab(g, text)
    return dict(PICK)


def pick_and_place(a, req, out, kind, name, bowl, place):
    """grab_text.py's pickup, then into the bowl (or the handover spot), then park."""
    look = (match_med(a.meds, name) if kind == "dispense" else None) or name
    how = f"looks_like:{look}" if look != name else "by_look"
    print(f"  -> picking '{look}'" + (" (dry run: find only)" if req.get("dry_run") else ""))
    pick = pick_like_grab_text(a, look, req.get("dry_run"))
    out.update(label=name.upper() if kind == "dispense" else name,
               score=round(pick["score"], 3) if pick.get("score") is not None else None)
    if "u" not in pick:
        out.update(detect_detail="not_found", error=pick.get("detail") or f"could not find {name}")
        return out
    try:
        xy = page(a, "click", {"u": pick["u"], "v": pick["v"], "size": pick["width"]})
        out["detect_detail"] = f"{how} at x={xy.get('x')}, y={xy.get('y')} cm"
    except Exception:
        out["detect_detail"] = how
    if req.get("dry_run"):
        out.update(ok=True, final_state="aborted", detect_detail=out["detect_detail"] + " (dry_run: no motion)")
        return out
    if pick["state"] != "HOLD DETECTED":
        try:
            page(a, "park")
        except Exception:
            pass
        out.update(detect_detail=out["detect_detail"] + f"; pick {pick['state'].lower()}",
                   error=f"pick failed ({pick['state']}): {pick.get('detail')}")
        return out
    where = "bowl" if place == "bowl" else "handover spot"
    note = put_down(a, bowl)
    try:
        page(a, "park")
    except Exception as e:
        print(f"  (couldn't park afterwards: {e})")
    seen, check = verify_place(a, look, bowl, place)
    print(f"  -> check: {check}")
    out.update(ok=True, final_state="placed", place_verified=seen,
               detect_detail=out["detect_detail"] + f"; put in {where}{note}; {check}")
    return out


PLATE_REACH_CM = 9.0     # with a fixed --bowl spot: "on it" = seen within this far from its middle


def verify_place(a, look, spot, place):
    """After parking, look once more with the standing camera: is the object on the plate now?
    Plate found by the camera: the object's base must lie inside the plate's outline in the picture.
    Fixed --bowl / handover spot: the object must be seen within PLATE_REACH_CM of it.
    Returns (True/False, what was seen). Never raises: a failed check just says so."""
    try:
        import time
        time.sleep(0.4)                                   # let the camera catch up after parking
        img = Image.open(io.BytesIO(requests.get(a.arm_url + "/frame.jpg", timeout=10).content)).convert("RGB")
        ts.VLM_BACKEND = "none"
        objs = [c for c in ts.decide(img, look, None)["candidates"] if c["target_score"] >= 0.35]
        if not objs:
            return False, "not verified: the object isn't seen any more"
        plate = None
        if place == "bowl" and not a.bowl:
            res = ts.decide(img, a.bowl_text, None)["candidates"]
            if res:
                plate = np.asarray(max(res, key=lambda c: c["target_score"])["mask"]).astype(bool)
        best = None
        for c in sorted(objs, key=lambda c: -c["target_score"]):
            m = np.asarray(c["mask"]).astype(bool)
            if plate is not None and m.shape == plate.shape and (m & plate).sum() > 0.5 * plate.sum():
                continue                                  # that's the (white) plate itself, not the object
            u, v = G.base_pixel(m)
            if plate is not None:
                h, w = plate.shape
                ui, vi = int(min(w - 1, max(0, u))), int(min(h - 1, max(0, v)))
                # inside the plate's outline (a few pixels of slack at its edge)
                near = plate[max(0, vi - 6):vi + 7, max(0, ui - 6):ui + 7].any()
                if near:
                    return True, "verified: seen on the plate"
            xy = page(a, "click", {"u": u, "v": v, "size": img.width})
            d = float(np.hypot(xy["x"] - spot[0], xy["y"] - spot[1]))
            if best is None or d < best[0]:
                best = (d, xy["x"], xy["y"])
            if plate is None and d <= PLATE_REACH_CM:
                return True, f"verified: seen {d:.1f} cm from the middle of the {'plate' if place == 'bowl' else 'spot'}"
        if best is None:
            return False, "not verified: the object isn't seen any more"
        return False, f"NOT on the plate: seen at x={best[1]:.1f}, y={best[2]:.1f} ({best[0]:.0f} cm away)"
    except Exception as e:      # the check must never turn a good place into an error
        return False, f"not verified (check failed: {type(e).__name__})"


def put_down(a, spot):
    """Put what the gripper holds down on the plate. If the arm can't finish the proper place (it stops
    short, e.g. a joint gets blocked), still let go: it is already above or near the plate by then,
    and an object stuck in the gripper is worse for the demo. Returns a note for the report."""
    print(f"  -> putting it down at x={spot[0]:.1f}, y={spot[1]:.1f}")
    try:
        page(a, "place", {"x": spot[0], "y": spot[1], "height": a.bowl_height, "above": a.above})
        return ""
    except Exception as e:
        print(f"  place stopped short ({e}); letting go where it is")
    try:
        page(a, "release")
        return " (the arm stopped short above it and let go there)"
    except Exception as e:
        raise RuntimeError(f"could not let go of the object: {e}")


def check_taken(req):
    """Whether the medicine was swallowed can't be seen from here: Buddy asks the person."""
    return {"schema": SCHEMA, "message_type": "check_taken_result", "request_id": req.get("request_id"),
            "ok": True, "taken": None, "via": "unavailable", "detail": "not checked by camera", "error": None}


def dispense(a, req, kind="dispense"):
    """One request from Buddy: a medicine (kind "dispense") or an everyday thing ("move_object")."""
    out = {"schema": SCHEMA, "message_type": kind + "_result", "request_id": req.get("request_id"),
           "ok": False, "final_state": "failed", "slot_id": None, "label": None, "score": None,
           "place_verified": False, "detect_detail": None, "error": None}
    if req.get("schema") != SCHEMA or req.get("message_type") != kind:
        out.update(final_state="aborted", error="unexpected schema or message_type")
        return out
    if kind == "dispense":
        name = ((req.get("medicine") or {}).get("name") or "").strip()
        place = "bowl"
    else:
        obj = req.get("object")
        name = " ".join(str((obj.get("label") if isinstance(obj, dict) else obj) or "").split())
        place = str(req.get("destination") or "bowl").lower().replace("the ", "").strip()
        if NOT_FOR_THE_ARM & set(name.lower().split()):
            out.update(final_state="aborted", error="I don't move sharp or hot things, or people")
            return out
        if place not in ("bowl", "handover") or (place == "handover" and not a.handover):
            out.update(final_state="aborted", error=f"I can't put things at the {place}; I can use the bowl"
                                                    + (" or the handover spot" if a.handover else ""))
            return out
    if not name:
        out.update(final_state="aborted", error="medicine.name is missing" if kind == "dispense" else "object.label is missing")
        return out
    if a.listen_only:
        out.update(final_state="aborted", label=name, detect_detail="listen-only: request received, nothing looked for or moved",
                   error="This is a connection test; the arm is not connected")
        return out
    if not LOCK.acquire(blocking=False):
        out.update(final_state="aborted", error="arm busy with another dispense")
        return out
    try:
        st = requests.get(a.arm_url + "/status", timeout=10).json()
        if st.get("read_only"):
            raise RuntimeError("arm controller is in read-only mode")
        basic = getattr(a, "basic", True)
        if basic:
            found = (locate(a, name, float(req.get("ocr_min_confidence", 0.65))) if kind == "dispense"
                     else locate_object(a, name))
        if place == "handover":
            bowl = (a.handover[0], a.handover[1])
        elif a.bowl:
            bowl = (a.bowl[0], a.bowl[1])
        else:   # find the bowl now, before the arm moves into the picture
            img = Image.open(io.BytesIO(requests.get(a.arm_url + "/frame.jpg", timeout=10).content)).convert("RGB")
            b = find_bowl(a, img)
            if b is None:
                out.update(detect_detail="bowl_not_found", error=f"could not find the {a.bowl_text}")
                return out
            bowl = (b[0], b[1])
        print(f"  plate at x={bowl[0]:.1f}, y={bowl[1]:.1f}")
        if not basic:
            return pick_and_place(a, req, out, kind, name, bowl, place)
        if found is None:
            out.update(detect_detail="not_found", error=f"could not find {name}")
            return out
        u, v, label, score, how = found
        xy = page(a, "click", {"u": u, "v": v, "size": 1280})
        out.update(label=label, score=round(score, 3),
                   detect_detail=f"{how} at x={xy.get('x')}, y={xy.get('y')} cm")
        if req.get("dry_run"):
            out.update(ok=True, final_state="aborted", detect_detail=out["detect_detail"] + " (dry_run: no motion)")
            return out
        got = page(a, "grab", {"u": u, "v": v, "size": 1280, "height": a.close if a.close is not None else 5.0,
                               "above": a.above, "method": "top"})
        if not got.get("holding"):
            page(a, "park")
            out.update(detect_detail=out["detect_detail"] + "; gripper closed on nothing",
                       error="pick failed: nothing in the gripper")
            return out
        page(a, "place", {"x": bowl[0], "y": bowl[1], "height": a.bowl_height, "above": a.above})
        page(a, "park")
        out.update(ok=True, final_state="placed", detect_detail=out["detect_detail"] + f"; put in {'bowl' if place == 'bowl' else 'handover spot'} (not verified)")
        return out
    except Exception as e:   # anything the arm or camera refused: report it, never pretend
        out.update(error=f"{type(e).__name__}: {e}")
        return out
    finally:
        LOCK.release()


def make_handler(a):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, data):
            body = json.dumps(data, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/v1/last", "/debug/last"):
                return self._send(200, LAST)
            if self.path == "/health":
                return self._send(200, {"ok": True, "schema": SCHEMA, "listen_only": a.listen_only,
                                        "routes": ["/v1/dispense", "/v1/move_object", "/v1/check_taken"]})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            kind = {"/v1/dispense": "dispense", "/v1/move_object": "move_object",
                    "/v1/check_taken": "check_taken"}.get(self.path)
            if kind is None:
                return self._send(404, {"error": "not found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._send(400, {"error": "body is not JSON"})
            if not isinstance(req, dict):
                return self._send(400, {"error": "body is not a JSON object"})
            if kind == "check_taken":
                return self._send(200, check_taken(req))
            LAST["request"], LAST["result"] = req, None
            print(f"{kind}: {req.get('medicine') or req.get('object')} dry_run={req.get('dry_run')}", flush=True)
            res = dispense(a, req, kind)
            LAST["result"] = res
            print(f"  -> {res['final_state']} ok={res['ok']} {res['detect_detail'] or ''} {res['error'] or ''}", flush=True)
            self._send(200, res)

        def log_message(self, *args):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meds", help='file with lines like "Aspirin = small bottle"')
    ap.add_argument("--read-labels", action="store_true",
                    help="with --meds: also try reading names on labels (slow)")
    ap.add_argument("--bowl-text", default="bowl", help='what the bowl looks like to SAM 3, e.g. "white bowl"')
    ap.add_argument("--bowl", nargs=2, type=float, metavar=("X", "Y"),
                    help="where the bowl stands, table x y in cm (read it off the arm page)")
    ap.add_argument("--handover", nargs=2, type=float, metavar=("X", "Y"),
                    help='hand-over spot, table x y in cm, for move_object with destination "handover"')
    ap.add_argument("--listen-only", action="store_true",
                    help="no arm, no camera: receive Reachy's requests, show them, answer 'not connected'")
    ap.add_argument("--bowl-height", type=float, default=8.0, help="let go this high above the table, cm")
    ap.add_argument("--close", type=float, default=None,
                    help="always close this high above the table, cm (default: grab_text decides per object; 5 with --basic)")
    ap.add_argument("--basic", action="store_true", help="old simple pick: fixed height, no wrist camera")
    ap.add_argument("--no-hand", action="store_true", help="grab_text pickup without the wrist camera")
    ap.add_argument("--above", type=float, default=14.0, help="approach / carry height, cm")
    ap.add_argument("--arm-url", default="http://localhost:8890")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from Buddy's laptop")
    ap.add_argument("--port", type=int, default=9090)
    a = ap.parse_args()
    if ts is None and not a.listen_only:
        ap.error("camera/arm packages are missing: pip install -r requirements.txt (or use --listen-only)")
    a.meds = load_meds(a.meds)
    print("Pickup: " + ("basic (fixed height, no wrist camera)" if a.basic else
                        "grab_text.py's " + str(getattr(G, "PICKUP_BUILD", "pickup"))
                        + (", wrist camera OFF" if a.no_hand else ", both cameras")))
    if a.meds:
        print("Medicines:", ", ".join(f"{k} -> {v}" for k, v in a.meds.items()))
    where = f"bowl at {a.bowl[0]}, {a.bowl[1]}" if a.bowl else f"finds the '{a.bowl_text}' in the camera"
    if a.listen_only:
        where = "LISTEN ONLY: nothing is looked for or moved"
    print(f"Medicine Buddy arm server on http://{a.host}:{a.port}  /v1/dispense  /v1/move_object  ({where})")
    ThreadingHTTPServer((a.host, a.port), make_handler(a)).serve_forever()


if __name__ == "__main__":
    main()
