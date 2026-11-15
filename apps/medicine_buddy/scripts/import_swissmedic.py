"""Build medicine_catalog.json from Swissmedic's "extended list of authorised medicinal products".

Source (public): https://www.swissmedic.ch/swissmedic/en/home/services/listen_neu.html
File: erweiterte_ham_ind.xlsx  (stdlib only, no extra dependencies)

    python scripts/import_swissmedic.py data/swissmedic/erweiterte_ham_ind.xlsx

Catalog shape:
  * one entry per single active ingredient (Latin pharmacopoeia name made readable,
    e.g. "metforminum" -> "Metformin"), Swiss brand names become its aliases;
  * combination products get one entry named after the brand;
  * the hand-written entries already in the catalog (and their aliases) are kept and win
    on name clashes.
Only marketed products taken by mouth are kept (see KEEP_CATEGORIES, is_oral; --all-forms lifts the
form filter): no vaccines, blood products, allergen extracts, homeopathics, radiopharmaceuticals,
injections, creams or export-only products.
Reference data only; it gives no dosing advice.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "medicine_buddy" / "medicine_catalog.json"
M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": M}

KEEP_CATEGORIES = {"Synthetikum", "Phytoarzneimittel", "Nicht-biologisches komplexes AM"}
COL = {"name": "C", "category": "F", "atc": "I", "ingredient": "O", "kind": "E", "status": "X"}

_STRENGTH_START = re.compile(r"^\d")
# Dosage forms a person takes at home by mouth; the arm hands over boxes, not injections or creams.
_ORAL = re.compile(
    r"tabl|kaps|dragée|dragee|compr|capsul|gélule|gelule|granul|pulver|poudre|sirup|sirop|tropfen|gouttes|saft"
    r"|einnehmen|buvable|lutsch|pastill|brause|effervesc|sachet|oral|pillol|cpr|lösung|solution|suspension",
    re.I,
)
_NOT_ORAL = re.compile(
    r"injek|infus|creme|crème|salbe|pommade|gel|lotion|augen|nasen|ohren|zäpf|suppos|spray|pflaster|inhal"
    r"|i\.v\.|i\.m\.|implant|emulsion|schaum|shampoo|vaginal|rektal|kutan|dermal|sublingual spray|ovul",
    re.I,
)


def is_oral(full_name: str) -> bool:
    form = full_name.split(",", 1)[1] if "," in full_name else ""
    return bool(_ORAL.search(form)) and not _NOT_ORAL.search(form)


def read_rows(path: Path) -> list[dict[str, str]]:
    z = zipfile.ZipFile(path)
    shared = [
        "".join(t.text or "" for t in si.iter(f"{{{M}}}t"))
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)
    ]
    rows = []
    for r in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).find("m:sheetData", NS).findall("m:row", NS):
        out = {}
        for c in r.findall("m:c", NS):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            v = c.find("m:v", NS)
            if v is not None:
                out[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(out)
    return rows


def ingredient_name(latin: str) -> str | None:
    """'metforminum' -> 'Metformin'; 'acidum acetylsalicylicum' -> 'Acetylsalicylic acid'."""
    words = latin.lower().strip().split()
    if not words:
        return None
    if words[0] == "acidum" and len(words) > 1:
        stem = re.sub(r"icum$", "ic", words[1])
        return f"{stem} acid".capitalize()
    base = words[0]
    base = re.sub(r"(um|ii|i|is)$", "", base) if len(base) > 5 else base
    return base.capitalize() if len(base) >= 4 else None


def brand_name(full: str) -> str | None:
    """'Sortis 10, Filmtabletten' -> 'Sortis'."""
    head = full.split(",")[0].strip()
    kept = []
    for tok in head.split():
        if _STRENGTH_START.match(tok):
            break
        kept.append(tok)
    name = " ".join(kept).strip(" -/")
    return name if len(name) >= 3 else None


def build(xlsx: Path, existing: list[dict], oral_only: bool = True) -> dict:
    rows = [r for r in read_rows(xlsx) if r.get(COL["name"]) and r.get(COL["status"])]
    owner: dict[str, str] = {}  # normalized key -> canonical entry name
    entries: dict[str, set[str]] = {}

    def claim(key: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", key.lower()).strip()

    for e in existing:  # hand-written entries first: they win clashes
        entries[e["name"]] = set(e.get("aliases", []))
        owner[claim(e["name"])] = e["name"]
        for a in e.get("aliases", []):
            owner.setdefault(claim(a), e["name"])

    brand_hits: dict[str, set[str]] = defaultdict(set)  # brand key -> entries that sell it
    brand_text: dict[str, str] = {}
    kept = 0
    for r in rows:
        if r.get(COL["kind"]) != "Hauptzulassung" or r.get(COL["category"]) not in KEEP_CATEGORIES:
            continue
        if oral_only and not is_oral(r[COL["name"]]):
            continue
        brand = brand_name(r[COL["name"]])
        latin = r.get(COL["ingredient"], "")
        single = latin and "," not in latin and " et " not in latin.lower()
        ing = ingredient_name(latin) if single else None
        target = None
        if ing:
            target = owner.get(claim(ing)) or owner.get(claim(ing + "e"))
            if target is None:
                target = ing
                entries.setdefault(target, set())
                owner[claim(target)] = target
        elif brand:
            target = owner.get(claim(brand)) or brand
            entries.setdefault(target, set())
            owner[claim(target)] = target
        if target is None:
            continue
        kept += 1
        if brand and claim(brand) != claim(target):
            brand_hits[claim(brand)].add(target)
            brand_text[claim(brand)] = brand

    for key, targets in brand_hits.items():
        if len(targets) != 1 or key in owner:  # ambiguous or clashes with a real name: skip
            continue
        (target,) = targets
        if key.startswith(claim(target) + " "):  # "Metformin Spirig HC": adds nothing over "Metformin"
            continue
        entries[target].add(brand_text[key])

    medicines = [{"name": n, "aliases": sorted(a)} for n, a in sorted(entries.items(), key=lambda kv: kv[0].lower())]
    print(f"products kept: {kept}; catalog entries: {len(medicines)}; aliases: {sum(len(m['aliases']) for m in medicines)}")
    return {
        "_note": (
            "The ONLY medicine names Medicine Buddy will accept. Generated from Swissmedic's extended list of "
            "authorised medicinal products by scripts/import_swissmedic.py (ingredient names are derived from Latin "
            "names; brand names are aliases). Hand-written demo entries come first and win clashes. "
            "Override with env MEDICINE_BUDDY_CATALOG=/path/to/file.json"
        ),
        "medicines": medicines,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--base", type=Path, default=None, help="hand-written catalog to keep (default: the demo copy)")
    ap.add_argument("--all-forms", action="store_true", help="also keep injections, creams, drops for eyes, etc.")
    args = ap.parse_args()
    base = args.base or args.out.with_name("medicine_catalog.demo.json")
    if not base.exists():
        base = args.out  # first run: keep what is there and save it as the demo copy
        base_data = json.loads(base.read_text(encoding="utf-8"))
        args.out.with_name("medicine_catalog.demo.json").write_text(
            json.dumps(base_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    existing = json.loads(base.read_text(encoding="utf-8"))["medicines"]
    args.out.write_text(json.dumps(build(args.xlsx, existing, oral_only=not args.all_forms), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
