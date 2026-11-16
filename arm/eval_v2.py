"""
eval_v2.py — evaluate selector v5 (or historical v3 with --selector v3).

tests_v1.csv columns:
  image, target, category, expected, x, y
    expected : GO, NOT_FOUND, AMBIGUOUS — or several allowed, e.g. GO|AMBIGUOUS
    x, y     : (for GO rows) a pixel ON the correct object, so we can check it picked
               the right one and not a look-alike. Leave empty if unknown.

Run:
  python eval_v2.py --tests tests_v1.csv --vlm none
  python eval_v2.py --tests tests_v1.csv --vlm ollama
  python eval_v2.py --label tests_v1.csv      # click the correct object for rows missing x,y

Result categories:
  PASS       correct
  WRONG_GO   said GO on the wrong object, or GO when it should not  -> DANGEROUS, must be 0
  MISSED     should be GO, but said NOT_FOUND / AMBIGUOUS             -> safe, but annoying
  MISMATCH   other wrong answer (e.g. NOT_FOUND vs AMBIGUOUS)          -> safe
"""
import argparse, csv, os, time
from PIL import Image
import target_selector_v5 as ts


def load_tests(path):
    with open(path, newline="") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    return [r for r in rows if r.get("image") and not r["image"].startswith("#")]


def picked_correct_object(res, x, y):
    if not (x and y):
        return None  # no ground-truth point given
    x, y = int(float(x)), int(float(y))
    m = res["chosen"]["mask"]
    if 0 <= y < m.shape[0] and 0 <= x < m.shape[1] and m[y, x]:
        return True
    return False  # Ground truth must be on the selected mask, not just inside its box.


def grade(row, res):
    expected = {e.strip().upper() for e in row["expected"].split("|")}
    got = res["status"]
    if got == "GO":
        if "GO" not in expected:
            return "WRONG_GO", "said GO but nothing should be picked"
        ok = picked_correct_object(res, row.get("x"), row.get("y"))
        if ok is False:
            return "WRONG_GO", "picked a different object"
        return ("PASS", "") if ok else ("UNVERIFIED", "GO without an object ground-truth point")
    if got in expected:
        return "PASS", ""
    if "GO" in expected:
        return "MISSED", f"expected GO, got {got}"
    return "MISMATCH", f"expected {row['expected']}, got {got}"


def run(tests_path, vlm, out_dir):
    ts.VLM_BACKEND = vlm
    os.makedirs(out_dir, exist_ok=True)
    rows = load_tests(tests_path)
    results, t0 = [], time.time()
    for i, row in enumerate(rows):
        img = Image.open(row["image"])
        t = time.time()
        res = ts.decide(img, row["target"], row["category"] or None)
        dt = time.time() - t
        verdict, note = grade(row, res)
        name = f'{i:02d}_{os.path.splitext(os.path.basename(row["image"]))[0]}_{row["target"].replace(" ", "-")}.jpg'
        ts.draw(img, res, os.path.join(out_dir, name))
        results.append((row, res["status"], verdict, note, dt))
        print(f'[{i+1}/{len(rows)}] {verdict:9s} {row["image"]:14s} "{row["target"]}" '
              f'-> {res["status"]}  ({dt:.1f}s) {note}')

    n = len(results)
    count = lambda v: sum(1 for r in results if r[2] == v)
    print("\n================ SUMMARY ================")
    print(f"checker: {vlm}   tests: {n}   total time: {time.time()-t0:.0f}s")
    print(f"PASS      {count('PASS'):3d}  ({100*count('PASS')/max(n,1):.0f}%)")
    print(f"WRONG_GO  {count('WRONG_GO'):3d}   <- must be 0 (robot would grab the wrong thing)")
    print(f"MISSED    {count('MISSED'):3d}   (safe: robot says it can't find it)")
    print(f"MISMATCH  {count('MISMATCH'):3d}")
    print(f"UNVERIFIED {count('UNVERIFIED'):2d}   (missing object labels; not counted as passes)")
    print(f"annotated images saved in: {out_dir}/")
    with open(os.path.join(out_dir, "report.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["image", "target", "expected", "got", "verdict", "note", "seconds"])
        for row, got, verdict, note, dt in results:
            w.writerow([row["image"], row["target"], row["expected"], got, verdict, note, f"{dt:.1f}"])


def label(tests_path):
    """Click the correct object for every GO row that has no x,y yet. Press any key to skip."""
    import cv2
    rows = load_tests(tests_path)
    fields = ["image", "target", "category", "expected", "x", "y"]
    for row in rows:
        if "GO" not in row["expected"].upper() or (row.get("x") and row.get("y")):
            continue
        img = cv2.imread(row["image"])
        win = f'Click the {row["target"]}  (any key = skip)'
        clicked = {}
        cv2.namedWindow(win)
        cv2.setMouseCallback(win, lambda e, x, y, *_: clicked.update(x=x, y=y)
                             if e == cv2.EVENT_LBUTTONDOWN else None)
        while not clicked:
            cv2.imshow(win, img)
            if cv2.waitKey(30) != -1:
                break
        cv2.destroyWindow(win)
        if clicked:
            row["x"], row["y"] = str(clicked["x"]), str(clicked["y"])
            print(f'{row["image"]} "{row["target"]}" -> ({row["x"]}, {row["y"]})')
    with open(tests_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"saved {tests_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", default="tests_v1.csv")
    ap.add_argument("--vlm", choices=["none", "ollama", "claude"], default="none")
    ap.add_argument("--selector", choices=["v3", "v5"], default="v5", help="v3 for the historical selector; v5 for current code")
    ap.add_argument("--out", default=None, help="folder for annotated images (default: eval_<vlm>)")
    ap.add_argument("--label", metavar="CSV", help="click correct objects to fill x,y in this CSV")
    a = ap.parse_args()
    if a.selector == "v3":
        import target_selector_v3 as ts
    if a.label:
        label(a.label)
    else:
        run(a.tests, a.vlm, a.out or f"eval_{a.vlm}")
