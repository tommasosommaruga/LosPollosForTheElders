"""Microphone -> local speech transcription -> selection in a SAVED IMAGE ONLY.

This demo does not import the arm controller or expose any motion/live-camera flag.
--text tests recognition without a microphone; --audio tests a saved recording.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


def normalize(text):
    return " ".join(re.sub(r"[^a-z0-9']+", " ", text.lower()).split())


def object_phrase(text):
    text = normalize(text)
    prefixes = ("please ", "can you ", "could you ", "would you ", "i want ", "i would like ",
                "give me ", "bring me ", "show me ", "find me ", "pick up ", "pick ",
                "fetch ", "bring ", "find ", "select ", "get ", "the ", "a ", "an ")
    while True:
        prefix = next((p for p in prefixes if text.startswith(p)), None)
        if prefix is None:
            break
        text = text[len(prefix):]
    for suffix in (" please", " for me"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return text.strip()


def match_item(text, items):
    words = normalize(text)
    if not words:
        return {"status": "NO_SPEECH"}
    if re.search(r"\b(no|not|don't|dont|never|stop|cancel)\b", words):
        return {"status": "CANCELLED"}
    names = {}
    for target, profile in items.items():
        for alias in [target, *profile.get("aliases", [])]:
            names.setdefault(normalize(alias), set()).add(target)
    parts = re.split(r"\b(?:and|or)\b", words)
    targets = set()
    for part in parts:
        found = names.get(object_phrase(part), set())
        if not found:
            return {"status": "UNKNOWN", "detail": "Use one of the configured object names; unknown qualifiers are not ignored."}
        targets.update(found)
    if len(targets) != 1:
        return {"status": "AMBIGUOUS_SPEECH", "choices": sorted(targets)}
    target = targets.pop()
    return {"status": "MATCHED", "target": target, "category": items[target]["category"]}


def describe_item(text):
    """Preserve an open-ended description; do not map it to a fixed catalog.

    This removes conversational wrappers only. SAM receives the full remaining
    description, including attributes and spatial language. It may not resolve
    every relational description correctly.
    """
    words = normalize(text)
    if not words:
        return {"status": "NO_SPEECH"}
    if re.search(r"\b(no|not|don't|dont|never|stop|cancel)\b", words):
        return {"status": "CANCELLED"}
    target = object_phrase(text)
    if not target or target in {"pick", "get", "select", "it", "that", "this", "something"}:
        return {"status": "NEEDS_DESCRIPTION"}
    return {"status": "MATCHED", "target": target, "category": None}


def select_from_text(text, image_path, items, output, vlm="none", selector=None,
                     vocabulary="open"):
    """Reusable entry point for a future Reachy text request. Never commands motion."""
    import target_selector_v5 as ts
    img = Image.open(image_path).convert("RGB")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    report = {"transcript": text, "image": str(Path(image_path).resolve()),
              "motion_allowed": False, "mode": "saved_image_selection",
              "vocabulary": vocabulary,
              **(match_item(text, items) if vocabulary == "catalog" else describe_item(text))}
    if report["status"] == "MATCHED":
        ts.VLM_BACKEND = vlm
        try:
            result = (selector or ts.decide)(img, report["target"], report["category"])
            report["selection"] = ts.summary(result)
            report["status"] = "SELECTED" if result["status"] == "GO" else result["status"]
            # SELECTED is a visual result, never a claim that the arm picked it up.
            ts.draw_sam3_style(img, {**result, "status": report["status"]}, str(root / "selection.jpg"), show_all=True)
        except Exception as exc:
            report.update(status="CHECK_FAILED", error=f"{type(exc).__name__}: {exc}")
    if "selection" not in report or report["status"] == "CHECK_FAILED":
        ImageDraw.Draw(img).text((15, 15), report["status"] + " - no object selected", fill="red", stroke_width=1, stroke_fill="white")
        img.save(root / "selection.jpg")
    report["message"] = {
        "SELECTED": "Found and highlighted the object in the photo. Nothing moved.",
        "UNKNOWN": "I don't recognize that object name. Please use a name from the list.",
        "NEEDS_DESCRIPTION": "Please describe the object you want to select.",
        "AMBIGUOUS_SPEECH": "Please request just one object.",
        "AMBIGUOUS": "More than one candidate fits. Please clarify; I will not guess.",
        "NOT_FOUND": "I could not find that object in this photo.",
        "NO_SPEECH": "No speech was detected. Please try again.",
        "CANCELLED": "Cancelled. Nothing selected or moved.",
        "CHECK_FAILED": "The visual check failed. No selection was approved.",
    }.get(report["status"], "Selection needs review.")
    (root / "result.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


class Listener:
    def __init__(self, model="base.en", download_root=None):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model, device="cpu", compute_type="int8", download_root=download_root)

    def transcribe(self, audio):
        segments, _ = self.model.transcribe(audio, language="en", vad_filter=True,
                                           condition_on_previous_text=False, beam_size=5)
        return " ".join(s.text.strip() for s in segments if s.no_speech_prob < .6).strip()


def record(seconds, device=None):
    import numpy as np
    import sounddevice as sd
    print(f"Listening for {seconds:g} seconds...", flush=True)
    audio = sd.rec(round(seconds * 16000), samplerate=16000, channels=1, dtype="float32", device=device)
    try:
        sd.wait()
    finally:
        sd.stop()
    audio = audio[:, 0]
    if not np.isfinite(audio).all() or np.max(np.abs(audio), initial=0) < .001:
        return None
    return audio


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="saved photo; default wrist1.jpeg")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="bypass speech recognition")
    source.add_argument("--audio", help="transcribe a saved audio file")
    parser.add_argument("--items", default=str(HERE / "voice_items.json"))
    parser.add_argument("--vocabulary", choices=["open", "catalog"], default="open",
                        help="open descriptions by default; catalog restores the original restricted demo")
    parser.add_argument("--vlm", choices=["none", "ollama"], default="none")
    parser.add_argument("--out", default=str(HERE / "review_outputs" / "voice_demo"))
    parser.add_argument("--whisper-model", default="base.en")
    parser.add_argument("--seconds", type=float, default=6)
    parser.add_argument("--device", type=int, help="microphone device number")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--show", action="store_true", help="open each result in the default image viewer")
    parser.add_argument("--once", action="store_true", help="stop after one microphone command")
    args = parser.parse_args(argv)
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if not 1 <= args.seconds <= 30:
        parser.error("--seconds must be between 1 and 30")
    photo = Path(args.image) if args.image else HERE / "wrist1.jpeg"
    if not photo.is_file():
        parser.error(f"Saved photo not found: {photo}")
    items = json.loads(Path(args.items).read_text()) if args.vocabulary == "catalog" else {}
    listener = None
    if args.text is None:
        print("Loading local speech recognition (first run may download the model)...", flush=True)
        listener = Listener(args.whisper_model, str(HERE / ".voice_models"))
    print("Photo:", photo)
    print("Describe the object you want in this photo." if args.vocabulary == "open"
          else "Known objects: " + ", ".join(items))
    print("IMAGE SELECTION ONLY. No arm connection or movement.")
    while True:
        if args.text is not None:
            text = args.text
        elif args.audio:
            text = listener.transcribe(args.audio)
        else:
            input("Press Enter when ready to speak (Ctrl+C to quit): ")
            audio = record(args.seconds, args.device)
            text = listener.transcribe(audio) if audio is not None else ""
        print(f"Heard: {text!r}", flush=True)
        result = select_from_text(text, photo, items, args.out, args.vlm,
                                  vocabulary=args.vocabulary)
        print(result["message"], flush=True)
        print("Preview:", Path(args.out) / "selection.jpg")
        if args.show:
            path = str(Path(args.out) / "selection.jpg")
            if sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                Image.open(path).show()
        if args.text is not None or args.audio or args.once:
            return result


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Nothing moved.")
