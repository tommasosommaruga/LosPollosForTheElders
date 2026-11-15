# Demo video graphics

Ready-to-use PNGs for the edit described in [../VIDEO_PLAN.md](../VIDEO_PLAN.md).

```
out/16x9/   1920×1080 — main film
out/9x16/   1080×1920 — vertical social cut (overlays sit higher, clear of app UI)
out/_contact-sheet-*.png — previews over a stand-in background
src/graphics.html — the single template (all wording lives in its GRAPHICS list)
src/render.py     — renders every graphic with headless Edge/Chrome
src/contact_sheet.py — rebuilds the previews (needs Pillow)
```

## What's here and where it goes

| File | Use at | How |
|---|---|---|
| `card-01-home` | 0:15, after the cold open | Full-screen, 2.5 s, dissolve in/out |
| `card-02-brand` | ~0:25, after "Good morning to you too" | Full-screen, 3 s |
| `card-03-asks` | ~1:25, end of the arm sequence | Full-screen, 2.5 s |
| `card-04a` → `04b` → `04c` → `04d` | 2:20 close | Hard-cut a→b→c→d (~1.5 s each, d holds 4 s); text never jumps, each step just adds a line |
| `label-01-confirmed` | Shot 11–13, after Mario says "Yes, please" | Overlay, ~2 s |
| `label-02-ocr` | Shot 14 (camera feed) | Overlay; edit the score in `graphics.html` to match what the system really reported |
| `label-03-placed` | Shot 16 (box lands in bowl) | Overlay |
| `label-04-taken` | Shot 18–19 | Overlay |
| `label-05`…`08` | Montage shots 29–32 | One per shot |
| `label-09-memory` | Shot 24/25, after "Yes — for Luca" | Overlay |
| `label-10-alert` | Shot 21 (phone buzz) | Overlay |
| `name-01`…`04` | First appearance of each person / Reachy | Overlay, 3 s |
| `stamp-01-first-day` | Shot 7 (setup flashback) | Top-left, 2 s |
| `stamp-02-nine` | Shot 10 (clock) | Top-left |
| `stamp-03-later` | Shot 27 (Luca match cut) | Top-left |
| `stamp-04-evening` | Shot 33 | Top-left |

Show at most **one overlay at a time**, and keep the calm feel by fading overlays in and out over
**8–12 frames** (in DaVinci Resolve: drag the PNG to V2 above the clip, then add a *Cross Dissolve*
to each end, or drag the clip's fade handles). Don't animate position — just fade.

In Resolve, set *Preferences → Editing → Standard still duration* to 3 s before importing so
stills land at a sensible length.

## Changing wording or adding graphics

1. Edit the `GRAPHICS` list (or `CLOSE_LINES`) in `src/graphics.html`. Open the file in a browser
   to preview: `graphics.html?id=label-02-ocr` (add `&fmt=9x16` for vertical).
2. Re-render everything, or only matching ids:

   ```bash
   python docs/demo-video/graphics/src/render.py
   ```

   ```bash
   python docs/demo-video/graphics/src/render.py label-02 card-04
   ```

3. Optional previews: `python docs/demo-video/graphics/src/contact_sheet.py`

Fonts (Source Serif 4 and Inter) come from Google Fonts at render time, so render while online.
Set `BROWSER_BIN` if the script can't find Edge or Chrome.

Palette: cream `#F0EEE6`, ink `#191919`, muted `#6B6A64`, clay accent `#C96442`.
