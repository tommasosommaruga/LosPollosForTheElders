"""Build preview contact sheets of every rendered PNG over a warm stand-in background.

Usage: python docs/demo-video/graphics/src/contact_sheet.py   (needs Pillow)
Writes out/_contact-sheet-16x9.png and out/_contact-sheet-9x16.png.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "out"
LAYOUTS = {"16x9": ((1920, 1080), 4, 480), "9x16": ((1080, 1920), 7, 270)}


def footage_stand_in(w: int, h: int) -> Image.Image:
    bg = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(bg)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=(90 + y * 60 // h, 75 + y * 40 // h, 60, 255))
    return bg


def main() -> None:
    for fmt, ((w, h), cols, tw) in LAYOUTS.items():
        files = sorted((OUT / fmt).glob("*.png"))
        if not files:
            continue
        th = int(tw * h / w)
        rows = (len(files) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * (tw + 10) + 10, rows * (th + 30) + 10), "#222")
        d = ImageDraw.Draw(sheet)
        base = footage_stand_in(w, h)
        for i, f in enumerate(files):
            frame = base.copy()
            frame.alpha_composite(Image.open(f).convert("RGBA"))
            x, y = 10 + (i % cols) * (tw + 10), 10 + (i // cols) * (th + 30)
            sheet.paste(frame.resize((tw, th)).convert("RGB"), (x, y))
            d.text((x, y + th + 4), f.stem, fill="#ddd")
        target = OUT / f"_contact-sheet-{fmt}.png"
        sheet.save(target)
        print(target)


if __name__ == "__main__":
    main()
