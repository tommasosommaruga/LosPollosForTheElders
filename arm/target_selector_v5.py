"""
target_selector.py — decide "is this the right object?" before the arm moves.

Pipeline
  1. SAM 3 proposes candidates from two prompts: the specific target ("blue pill box")
     and a broader category ("box"), so a slightly-off phrase doesn't make us miss it.
  2. Exact-target SAM scores select matches by default. An optional checker can
     inspect crops only when explicitly enabled with --vlm.
  3. Decision rules:
       exactly 1 confident match          -> GO
       0 matches                          -> NOT_FOUND   (Reachy: "I can't find it")
       2+ matches                         -> AMBIGUOUS   (Reachy asks / use position hint)
  4. Consistency: run on N frames, only GO if every frame picks the same object.

Usage
  python target_selector.py --image table.jpg --target "blue pill box" --category "box"
  python target_selector.py --camera 0 --target "blue pill box" --category "box" --frames 3
  python target_selector.py --image table.jpg --target "blue pill box" --category "box" --vlm none
     (default: SAM 3 only; no separate model service needed)

Needs: pip install -r requirements.txt
       (on a Mac, the standard torch wheel includes Apple GPU / MPS support)
       hf auth login   (after accepting the conditions on huggingface.co/facebook/sam3)
       Optional checkers are described in docs/OPTIONAL_CHECKERS.md.
"""
import os
# On Apple Silicon, let any op that MPS doesn't support fall back to CPU instead of crashing.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
import argparse, base64, io, json, sys, math
import numpy as np
import cv2
from PIL import Image

# ---------------- config ----------------
SAM_SCORE_MIN = 0.35      # retain proposals; exact-target matching is evaluated below
VLM_CONF_MIN = 0.80       # VLM must be this sure to count as a match
SAM_ONLY_MIN = 0.50       # with --vlm none: SAM score needed to count as a match
IOU_DUP = 0.6             # merge duplicate candidates from the two prompts
CROP_PAD = 0.25           # context around each crop (fraction of box size)
VLM_MODEL = "claude-sonnet-5-5"

# ---------------- SAM 3 ----------------
_processor = _model = None
_device = None
def sam3_candidates(img: Image.Image, prompts):
    # Uses the Hugging Face Transformers integration (model card: huggingface.co/facebook/sam3)
    global _processor, _model, _device
    import torch
    if _model is None:
        from transformers import Sam3Processor, Sam3Model
        if torch.cuda.is_available():
            _device = "cuda"
        elif torch.backends.mps.is_available():   # Apple Silicon (M1/M2/M3)
            _device = "mps"
        else:
            _device = "cpu"
        print(f"SAM 3 running on: {_device}")
        _model = Sam3Model.from_pretrained("facebook/sam3").to(_device).eval()
        _processor = Sam3Processor.from_pretrained("facebook/sam3")
    img = img.convert("RGB")
    cands = []
    for p in prompts:
        inputs = _processor(images=img, text=p, return_tensors="pt").to(_device)
        with torch.no_grad():
            outputs = _model(**inputs)
        out = _processor.post_process_instance_segmentation(
            outputs, threshold=SAM_SCORE_MIN, mask_threshold=0.5,
            target_sizes=inputs.get("original_sizes").tolist())[0]
        for m, b, s in zip(out["masks"], out["boxes"], out["scores"]):
            s = float(s)
            if s < SAM_SCORE_MIN:
                continue
            m = np.asarray(m.cpu() if hasattr(m, "cpu") else m).squeeze().astype(bool)
            b = [float(v) for v in (b.cpu() if hasattr(b, "cpu") else b)]
            cands.append({"mask": m, "box": b, "sam_score": s, "prompt": p,
                          "is_target_prompt": p == prompts[0]})
    return dedupe(cands)

def box_iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area = lambda r: (r[2] - r[0]) * (r[3] - r[1])
    return inter / (area(a) + area(b) - inter + 1e-9)

def dedupe(cands):
    """Merge duplicates found by both prompts; remember every prompt that found each object
    and the best score it got from the specific-target prompt (target_score)."""
    cands = sorted(cands, key=lambda c: -c["sam_score"])
    kept = []
    for c in cands:
        # Overlapping boxes may contain different objects. Require mask overlap too.
        def same_instance(k):
            if box_iou(c["box"], k["box"]) < IOU_DUP:
                return False
            a, b = c["mask"].astype(bool), k["mask"].astype(bool)
            return a.shape == b.shape and np.count_nonzero(a & b) / max(1, np.count_nonzero(a | b)) >= IOU_DUP
        dup = next((k for k in kept if same_instance(k)), None)
        if dup is None:
            c["prompts"] = {c["prompt"]}
            c["target_score"] = c["sam_score"] if c.get("is_target_prompt") else 0.0
            kept.append(c)
        else:
            dup["prompts"].add(c["prompt"])
            if c.get("is_target_prompt"):
                dup["target_score"] = max(dup["target_score"], c["sam_score"])
    return kept

# ---------------- VLM check ----------------
_client = None
def crop(img: Image.Image, box):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    x1 = max(0, x1 - CROP_PAD * w); y1 = max(0, y1 - CROP_PAD * h)
    x2 = min(img.width, x2 + CROP_PAD * w); y2 = min(img.height, y2 + CROP_PAD * h)
    return img.crop((int(x1), int(y1), int(x2), int(y2)))

def to_b64(im: Image.Image):
    buf = io.BytesIO(); im.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()

VLM_BACKEND = "none"            # SAM-only unless a checker is explicitly requested
OLLAMA_MODEL = "qwen2.5vl:7b"   # set by --vlm-model
OLLAMA_URL = "http://localhost:11434/api/chat"

def parse_json(text):
    text = text.strip()
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:
        return {"match": False, "confidence": 0.0, "reason": f"unparseable: {text[:80]}", "error": True}

def validate_match(value):
    """Reject malformed model output rather than coercing it into a positive match."""
    confidence = value.get("confidence") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("error") or type(value.get("match")) is not bool
            or type(confidence) not in (int, float) or not math.isfinite(confidence)
            or not 0 <= confidence <= 1):
        return {"match": False, "confidence": 0.0, "reason": "invalid VLM response", "error": True}
    return {"match": value["match"], "confidence": float(confidence),
            "reason": str(value.get("reason", ""))}


MATCH_SCHEMA = {"type": "object", "properties": {
    "match": {"type": "boolean"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "reason": {"type": "string"}}, "required": ["match", "confidence", "reason"],
    "additionalProperties": False}


def ollama_check(crop_img: Image.Image, prompt: str, schema=None):
    import requests
    r = requests.post(OLLAMA_URL, timeout=120, json={
        "model": OLLAMA_MODEL, "stream": False, "format": schema or "json",
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": prompt, "images": [to_b64(crop_img)]}],
    })
    r.raise_for_status()
    return parse_json(r.json()["message"]["content"])

def vlm_check(crop_img: Image.Image, target: str):
    global _client
    prompt = (
        f'A robot must pick up exactly: "{target}".\n'
        "Is the main object in this image that exact item? Be strict: similar-looking "
        "but different items (other color, other label, other box) are NOT a match.\n"
        'Reply with JSON only: {"match": true/false, "confidence": 0.0-1.0, "reason": "short"}'
    )
    if VLM_BACKEND == "ollama":
        return ollama_check(crop_img, prompt, MATCH_SCHEMA)
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    msg = _client.messages.create(
        model=VLM_MODEL, max_tokens=200,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": to_b64(crop_img)}},
            {"type": "text", "text": prompt}]}])
    return parse_json(msg.content[0].text)

# ---------------- decision ----------------
def decide(img: Image.Image, target: str, category: str | None):
    prompts = [target] + ([category] if category else [])
    cands = sam3_candidates(img, prompts)
    for c in cands:
        if VLM_BACKEND == "none":
            # SAM-only: trust SAM's own score for the specific target phrase
            ok = c["target_score"] >= SAM_ONLY_MIN
            c["vlm"] = {"match": ok, "confidence": c["target_score"],
                        "reason": "SAM only: score for the exact target phrase"}
            c["is_match"] = ok
            continue
        v = validate_match(vlm_check(crop(img, c["box"]), target))
        c["vlm"] = v
        c["is_match"] = bool(v.get("match")) and float(v.get("confidence", 0)) >= VLM_CONF_MIN
    matches = [c for c in cands if c["is_match"]]
    if any(c["vlm"].get("error") for c in cands):
        status = "CHECK_FAILED"
    elif len(matches) == 1:
        status = "GO"
    elif not matches:
        status = "NOT_FOUND"
    else:
        status = "AMBIGUOUS"
    return {"status": status, "candidates": cands, "chosen": matches[0] if status == "GO" else None}

def mask_center(mask):
    ys, xs = np.nonzero(mask)
    return float(xs.mean()), float(ys.mean())

def consistent(results):
    """GO only if every frame said GO and the chosen object stayed in the same place."""
    if not results or not all(r["status"] == "GO" for r in results):
        return False
    centers = [mask_center(r["chosen"]["mask"]) for r in results]
    c0 = np.array(centers[0])
    return all(np.linalg.norm(np.array(c) - c0) < 20 for c in centers)  # pixels

# ---------------- output ----------------
PX_PER_CM = None   # set by --px-per-cm: image scale at the table, to report sizes in cm

def shape_info(mask):
    """Size of the object's outline: length/width of the tightest rotated rectangle."""
    m8 = mask.astype(np.uint8)
    cnts, _ = cv2.findContours(m8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    (cx, cy), (w, h), ang = cv2.minAreaRect(c)
    info = {"center_px": [round(cx), round(cy)],
            "length_px": round(max(w, h), 1), "width_px": round(min(w, h), 1),
            "angle_deg": round(ang if w >= h else ang + 90, 1),
            "area_px": int(m8.sum())}
    if PX_PER_CM:
        info["length_cm"] = round(info["length_px"] / PX_PER_CM, 1)
        info["width_cm"] = round(info["width_px"] / PX_PER_CM, 1)
    return info

def draw(img: Image.Image, res, path):
    """Each object drawn in its own shape: green fill = accepted, red outline = rejected."""
    vis = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    overlay = vis.copy()
    labels = []
    for i, c in enumerate(res["candidates"]):
        m = c["mask"].astype(np.uint8)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        if c["is_match"]:
            overlay[m.astype(bool)] = (0, 200, 0)
            cv2.drawContours(vis, cnts, -1, (0, 255, 0), 3)
        else:
            cv2.drawContours(vis, cnts, -1, (0, 0, 220), 2)
        top = min(cnts[0][:, 0, :], key=lambda p: p[1])          # topmost point of outline
        label = (f'#{i} target={c["target_score"]:.2f} '
                 f'check={float(c["vlm"].get("confidence", 0)):.2f}')
        labels.append((label, (int(top[0]) - 40, max(15, int(top[1]) - 8)),
                       (0, 200, 0) if c["is_match"] else (0, 0, 220)))
    vis = cv2.addWeighted(overlay, 0.4, vis, 0.6, 0)
    for text, pos, col in labels:
        cv2.putText(vis, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 2)
    if res["chosen"] is not None:
        info = shape_info(res["chosen"]["mask"])
        if info:
            cv2.circle(vis, tuple(info["center_px"]), 6, (255, 255, 255), -1)
    cv2.putText(vis, res["status"], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    cv2.imwrite(path, vis)

# Same look as SAM 3's own plot_results (sam3/visualization_utils.py):
#   one distinct color per object (palette built like theirs: 5000 random RGB, seed 42,
#   128 k-means clusters in LAB space), mask filled at 50% opacity, thin box in the
#   same color, score label on a 75% white tag, 5 px above.
SAM3_COLORS = [(49, 150, 141), (93, 36, 115), (163, 176, 52), (179, 118, 49), (227, 49, 205),
               (76, 188, 26), (172, 87, 67), (144, 70, 226), (223, 199, 207), (100, 83, 115),
               (202, 41, 35), (53, 148, 225), (46, 118, 65), (95, 236, 157), (235, 152, 187),
               (227, 52, 107), (225, 222, 116), (70, 62, 29), (153, 129, 238), (63, 242, 35),
               (228, 222, 158), (180, 27, 110), (46, 21, 144), (57, 224, 197)]   # RGB

def draw_sam3_style(img: Image.Image, res, path, show_all=False):
    """SAM 3 look. show_all=False: only accepted objects. show_all=True: every object SAM 3
    found gets its own color + mask + box + label; accepted ones get a thicker box and MATCH."""
    rgb = np.array(img.convert("RGB")).astype(np.float32)
    shown = [(i, c) for i, c in enumerate(res["candidates"]) if c["is_match"] or show_all]
    for i, c in shown:                                   # 1) masks: color at 50% opacity
        col = np.array(SAM3_COLORS[i % len(SAM3_COLORS)], np.float32)
        m = c["mask"].astype(bool)
        rgb[m] = 0.5 * rgb[m] + 0.5 * col
    vis = cv2.cvtColor(rgb.clip(0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    font = cv2.FONT_HERSHEY_SIMPLEX
    for i, c in shown:                                   # 2) boxes + labels
        r, g, b = SAM3_COLORS[i % len(SAM3_COLORS)]
        col = (b, g, r)
        x1, y1, x2, y2 = map(int, c["box"])
        cv2.rectangle(vis, (x1, y1), (x2, y2), col, 4 if (c["is_match"] and show_all) else 2)
        text = f"id={i}, target={c['target_score']:.2f}"
        if show_all:
            reasons = c.get("decision", {}).get("reasons", [])
            selected = c is res.get("chosen")
            text += "  SELECTED" if selected else "  MATCH" if c["is_match"] else "  " + (reasons[0] if reasons else "no")
        (tw, th), _ = cv2.getTextSize(text, font, 0.5, 2)
        ty = max(th + 4, y1 - 5)
        tag = vis[ty - th - 3:ty + 3, x1:x1 + tw + 4]
        if tag.size:
            vis[ty - th - 3:ty + 3, x1:x1 + tw + 4] = (0.25 * tag + 0.75 * 255).astype(np.uint8)
        cv2.putText(vis, text, (x1 + 2, ty), font, 0.5, col, 2)
    matches = [str(i) for i, c in enumerate(res["candidates"]) if c is res.get("chosen")]
    status = res["status"] + (f"  -> id={','.join(matches)}" if matches else "")
    cv2.putText(vis, status, (10, 30), font, 1.0, (255, 255, 255), 4)
    cv2.putText(vis, status, (10, 30), font, 1.0, (0, 0, 0), 2)
    cv2.imwrite(path, vis)

def summary(res):
    out = {"status": res["status"], "candidates": [
        {"i": i, "box": [round(v) for v in c["box"]], "found_by": sorted(c["prompts"]),
         "sam_score": round(c["sam_score"], 2), "target_score": round(c["target_score"], 2),
         "vlm": c["vlm"]}
        for i, c in enumerate(res["candidates"])]}
    if res["chosen"] is not None:
        out["target_pixel"] = [round(v) for v in mask_center(res["chosen"]["mask"])]
        out["target_shape"] = shape_info(res["chosen"]["mask"])
    if "decision_reason" in res:
        out["chosen_candidate_id"] = res["chosen"]["decision"]["candidate_id"] if res["chosen"] else None
        out["decision_reason"] = res["decision_reason"]
        out["search_scope"] = res["search_scope"]
        out["pick_at_px"] = res.get("pick_at_px")
        for summary, candidate in zip(out["candidates"], res["candidates"]):
            summary["decision"] = candidate["decision"]
    return out

def grab_frames(cam, n):
    cap = cv2.VideoCapture(cam)
    for _ in range(5): cap.read()  # let exposure settle
    frames = []
    for _ in range(n):
        ok, f = cap.read()
        if not ok: sys.exit("camera read failed")
        frames.append(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)))
    cap.release()
    return frames

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--image"); ap.add_argument("--camera", type=int)
    ap.add_argument("--target", required=True); ap.add_argument("--category")
    ap.add_argument("--frames", type=int, default=1)
    ap.add_argument("--out", default="decision.jpg")
    ap.add_argument("--vlm", choices=["claude", "ollama", "none"], default="none",
                    help="SAM-only by default; optional checkers require explicit setup")
    ap.add_argument("--vlm-model", default=OLLAMA_MODEL, help="Ollama model name")
    ap.add_argument("--px-per-cm", type=float, help="image scale at the table, to report cm")
    ap.add_argument("--style", choices=["sam3", "simple"], default="sam3",
                    help="sam3 = same look as SAM 3's own visualisation; simple = green/red v3 style")
    ap.add_argument("--show-all", action="store_true",
                    help="draw every object SAM 3 found in full colour; matches marked MATCH")
    a = ap.parse_args()
    VLM_BACKEND, OLLAMA_MODEL, PX_PER_CM = a.vlm, a.vlm_model, a.px_per_cm
    print(f"Checker: {VLM_BACKEND}" + (f" ({OLLAMA_MODEL})" if VLM_BACKEND == "ollama" else ""))

    frames = [Image.open(a.image)] * 1 if a.image else grab_frames(a.camera, a.frames)
    results = [decide(f, a.target, a.category) for f in frames]
    final = results[-1]
    if len(results) > 1 and final["status"] == "GO" and not consistent(results):
        final = {**final, "status": "AMBIGUOUS", "chosen": None}
    if a.style == "sam3":
        draw_sam3_style(frames[-1], final, a.out, a.show_all)
    else:
        draw(frames[-1], final, a.out)
    print(json.dumps(summary(final), indent=2))
