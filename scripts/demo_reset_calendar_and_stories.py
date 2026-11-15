#!/usr/bin/env python3
"""Demo reset: wipe calendar junk + seed a few clean stories/events.

Clears local ``calendar_events`` (and optionally every event on the connected
Google calendar), then seeds a light demo:

- People: Caregiver Anna, Adarsh, Tommaso (created only if missing)
- Memories: one rich Adarsh story; one Tommaso story (duplicates removed)
- Schedules: one Paracetamol for Adarsh, one Vitamin D for Tommaso
- Companion calendar: two clean local events (optionally pushed to Google)

Usage (robot DB + Google wipe + seed)::

    # on the Reachy (apps_venv already has medicine_buddy + .env keys):
    set -a && source ~/.local/share/medicine-buddy/.env && set +a
    export MEDICINE_BUDDY_DB=$HOME/.local/share/medicine-buddy/medicine_buddy.db
    /venvs/apps_venv/bin/python /tmp/demo_reset_calendar_and_stories.py \\
        --wipe-local-calendar --wipe-google --seed --yes

From the laptop (uses repo .env + checkout DB unless MEDICINE_BUDDY_DB is set)::

    cd apps/medicine_buddy && source .venv/bin/activate
    python ../../scripts/demo_reset_calendar_and_stories.py --seed --yes
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "medicine_buddy"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


ADARSH_STORY = (
    "My name is Adarsh. When I was a boy in Pune, my grandmother woke before sunrise "
    "to grind spices for the day's meals. The kitchen smelled of cumin and coriander, "
    "and she let me sit on a low stool and watch. Every Sunday she made poha for the "
    "whole family — flattened rice with mustard seeds, curry leaves, peanuts, and a "
    "squeeze of lemon. She said food tastes better when you cook it for people you love. "
    "Years later, when I moved to Switzerland for work, I still make her poha on quiet "
    "Sunday mornings. The mustard seeds crackle the same way. I close my eyes and I am "
    "back in her kitchen for a moment. I want my children and grandchildren to know that "
    "story — not just the recipe, but how patient and warm she was with me."
)

TOMMASO_STORY = (
    "I am Tommaso. Every summer as a child we went to my nonna's village in the hills. "
    "In the evening the old men played cards under the mulberry tree, and Nonna sent me "
    "with a basket to pick ripe figs before the birds got them. She taught me to twist "
    "the fruit gently so the tree would keep giving. After dinner she told the same "
    "stories about the war and the neighbours, and I pretended I had never heard them. "
    "Now when I smell warm figs I think of that courtyard, the dusty road, and her laugh. "
    "That is the memory I want Reachy to keep for my family."
)


def _ensure_person(db, name: str, role: str, phrase: str):
    from medicine_buddy.auth import make_secret

    existing = db.get_person_by_name(name)
    if existing is not None:
        return existing
    return db.add_person(name, role, make_secret(phrase))


def _replace_stories(db, person, *, title: str, text: str) -> None:
    for mem in list(db.list_memories(person_id=person.id, limit=200)):
        db.delete_memory(mem.id)
    db.add_memory(person.id, "story", title, text)


def _wipe_local_calendar(db) -> int:
    rows = db.list_calendar_events(status=None, limit=500)
    n = 0
    for ev in rows:
        if db.delete_calendar_event(ev.id):
            n += 1
    # Second pass in case of pagination/limit leftovers.
    while True:
        left = db.list_calendar_events(status=None, limit=500)
        if not left:
            break
        for ev in left:
            db.delete_calendar_event(ev.id)
            n += 1
    return n


def _seed_schedules(db, adarsh, tommaso) -> None:
    from medicine_buddy.calendar_mirror import mirror_schedule

    for person, med_name, weekdays, time_hhmm in (
        (adarsh, "Paracetamol", "0,2,4", "09:00"),
        (tommaso, "Vitamin D", "*", "10:00"),
    ):
        med = db.get_medicine_by_name(med_name, person.id)
        if med is None:
            med = db.add_medicine(med_name, person.id)
        existing = db.find_active_schedule(med.id, weekdays, time_hhmm)
        if existing is None:
            schedule = db.add_schedule(med.id, weekdays, time_hhmm)
        else:
            schedule = existing
        mirror_schedule(db, schedule, med)


def _seed_companions(db, *, push_google: bool) -> list[int]:
    from medicine_buddy.calendar_ics import new_uid
    from medicine_buddy.google_calendar import (
        build_google_event_body,
        insert_google_event,
        load_google_cal_config,
        ensure_timezone_name,
    )

    specs = (
        ("Garden walk", "1,3", "10:00", "A gentle walk if the weather is kind."),
        ("Family call", "6", "16:00", "Call the children — keep it short and warm."),
    )
    created: list[int] = []
    cfg = load_google_cal_config(db.get_setting("google_calendar_id"))
    tz_name = ensure_timezone_name(None)
    tz_setting = (db.get_setting("timezone") or "").strip()
    if tz_setting:
        tz_name = tz_setting
    for title, weekdays, time_hhmm, description in specs:
        ev = db.upsert_calendar_event(
            uid=new_uid("local"),
            kind="companion",
            title=title,
            description=description,
            weekdays=weekdays,
            time_hhmm=time_hhmm,
            source="local",
            status="active",
        )
        created.append(ev.id)
        if push_google and cfg.configured:
            body = build_google_event_body(
                title=ev.title,
                description=ev.description,
                time_hhmm=ev.time_hhmm,
                weekdays=ev.weekdays,
                starts_at=ev.starts_at or "",
                all_day=False,
                tz_name=tz_name,
            )
            try:
                gid = insert_google_event(cfg, body)
                db.set_calendar_external_id(ev.id, gid)
            except Exception as exc:  # noqa: BLE001
                print(f"  warn: could not push “{title}” to Google: {exc}")
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite path (default: MEDICINE_BUDDY_DB or medicine_buddy default)",
    )
    parser.add_argument(
        "--wipe-local-calendar",
        action="store_true",
        help="DELETE all rows in calendar_events",
    )
    parser.add_argument(
        "--wipe-google",
        action="store_true",
        help="Delete every event on the configured Google calendar",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed Adarsh/Tommaso stories + light meds + 2 companion events",
    )
    parser.add_argument(
        "--push-google",
        action="store_true",
        help="After seed, push the 2 companion events to Google",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    _load_dotenv(ROOT / ".env")
    robot_env = Path.home() / ".local" / "share" / "medicine-buddy" / ".env"
    _load_dotenv(robot_env)

    from medicine_buddy.db import Database, default_db_path
    from medicine_buddy.google_calendar import load_google_cal_config, wipe_google_calendar_events

    db_path = args.db or Path(os.environ.get("MEDICINE_BUDDY_DB") or default_db_path())
    print(f"Database: {db_path}")
    if not args.wipe_local_calendar and not args.wipe_google and not args.seed:
        parser.error("Pick at least one of --wipe-local-calendar / --wipe-google / --seed")

    if not args.yes:
        bits = []
        if args.wipe_local_calendar:
            bits.append("wipe LOCAL calendar_events")
        if args.wipe_google:
            bits.append("wipe ALL events on GOOGLE calendar")
        if args.seed:
            bits.append("seed demo people/stories/schedules/events")
        answer = input(f"Proceed ({', '.join(bits)})? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted.")
            return 1

    db = Database(db_path)
    try:
        if args.wipe_google:
            cfg = load_google_cal_config(db.get_setting("google_calendar_id"))
            if not cfg.configured:
                print("Google not configured — skip --wipe-google "
                      "(need client/secret/refresh + calendar id).")
            else:
                n = wipe_google_calendar_events(cfg)
                print(f"Google: deleted {n} event(s) from {cfg.calendar_id}")

        if args.wipe_local_calendar:
            n = _wipe_local_calendar(db)
            print(f"Local calendar_events: deleted {n} row(s)")

        if args.seed:
            if not db.has_caregiver():
                anna = _ensure_person(db, "Anna", "caregiver", "purple mountain sunrise")
                print(f"Created caregiver {anna.name}")
            else:
                print("Caregiver already present")
            adarsh = _ensure_person(db, "Adarsh", "person", "old fishing boat")
            tommaso = _ensure_person(db, "Tommaso", "person", "quiet garden path")
            _replace_stories(
                db,
                adarsh,
                title="Grandmother's Sunday poha",
                text=ADARSH_STORY,
            )
            _replace_stories(
                db,
                tommaso,
                title="Figs at Nonna's village",
                text=TOMMASO_STORY,
            )
            print(
                f"Stories: Adarsh → 1 detailed story; Tommaso → 1 story "
                f"(removed older duplicates for each)"
            )
            _seed_schedules(db, adarsh, tommaso)
            print("Schedules: Adarsh Paracetamol Mon/Wed/Fri 09:00; "
                  "Tommaso Vitamin D daily 10:00")
            ids = _seed_companions(db, push_google=args.push_google)
            print(
                f"Companion events: Garden walk + Family call "
                f"(ids {ids}"
                f"{', pushed to Google' if args.push_google else ', local only'})"
            )
            if not (db.get_setting("timezone") or "").strip():
                db.set_setting("timezone", "Europe/Zurich")
                print("Timezone set to Europe/Zurich")
    finally:
        db.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
