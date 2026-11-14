"""CLI entrypoints for Medicine Buddy."""

from __future__ import annotations

import argparse
from pathlib import Path

from medicine_buddy.config import AppConfig
from medicine_buddy.db import DEFAULT_DB_PATH, Database
from medicine_buddy.dry_run import ConsoleBuddy, FakeAdherence, FakeArm, FakeVision
from medicine_buddy.orchestrator import DoseStateMachine
from medicine_buddy.scheduler import find_due_doses
from medicine_buddy.voice_setup import setup_medicine_schedule


def _build_dry_run_stack(db_path: Path, config: AppConfig) -> DoseStateMachine:
    db = Database(db_path)
    buddy = ConsoleBuddy()
    vision = FakeVision(config)
    arm = FakeArm()
    adherence = FakeAdherence(buddy)
    return DoseStateMachine(db, buddy, vision, arm, adherence, config)


def cmd_demo(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("Hardware mode is not wired in v0.1. Re-run with --dry-run.")
        return 2

    config = AppConfig(dry_run=True, adherence_timeout_seconds=args.timeout)
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    machine = _build_dry_run_stack(db_path, config)
    db = machine.db
    buddy = machine.buddy

    buddy.speak("Medicine Buddy dry-run demo. Let's set up one medicine.")
    name = args.medicine or "Aspirin"
    weekdays = args.weekdays or "daily"
    time_hhmm = args.time or "09:00"

    medicine, _schedule = setup_medicine_schedule(
        db,
        buddy,
        name=name,
        weekdays_raw=weekdays,
        time_raw=time_hhmm,
    )

    # Demo force-triggers after setup so you don't wait for the clock.
    buddy.speak(f"Force-triggering due dose for {medicine.name}.")
    final = machine.run_dose(medicine.id)
    buddy.speak(f"Final state: {final.value}")
    print("\nDose event log:")
    for event in db.list_dose_events(medicine.id):
        slot = f" slot={event.slot_id}" if event.slot_id is not None else ""
        print(f"  [{event.created_at}] {event.state.value}{slot} — {event.detail}")
    db.close()
    return 0


def cmd_check_due(args: argparse.Namespace) -> int:
    db = Database(Path(args.db) if args.db else DEFAULT_DB_PATH)
    due = find_due_doses(db)
    if not due:
        print("No doses due this minute.")
    else:
        for item in due:
            print(
                f"DUE medicine_id={item.medicine_id} "
                f"name={item.medicine_name} at {item.time_hhmm}"
            )
    db.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from medicine_buddy.api import serve

    config = AppConfig(dry_run=bool(args.dry_run))
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    serve(host=args.host, port=args.port, db_path=db_path, config=config,
          voice_guard=bool(args.voice_guard))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="medicine-buddy")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Interactive dry-run dispense demo")
    demo.add_argument("--dry-run", action="store_true", default=True)
    demo.add_argument("--db", default="")
    demo.add_argument("--medicine", default="Aspirin")
    demo.add_argument("--weekdays", default="daily")
    demo.add_argument("--time", default="09:00")
    demo.add_argument("--timeout", type=float, default=5.0)
    demo.add_argument("--force-due", action="store_true", default=True)
    demo.set_defaults(func=cmd_demo)

    check = sub.add_parser("check-due", help="List schedules due this minute")
    check.add_argument("--db", default="")
    check.set_defaults(func=cmd_check_due)

    serve_cmd = sub.add_parser(
        "serve",
        help="HTTP API for reachy_mini_conversation external tools (other PC)",
    )
    serve_cmd.add_argument("--host", default="0.0.0.0")
    serve_cmd.add_argument("--port", type=int, default=8787)
    serve_cmd.add_argument("--db", default="")
    serve_cmd.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use fake arm/vision (default true)",
    )
    serve_cmd.add_argument(
        "--voice-guard",
        action="store_true",
        default=False,
        help="Only allow medicine changes the person actually asked for out loud "
        "(prompt-injection protection; the Reachy Mini app medicine_buddy.main turns it on itself)",
    )
    serve_cmd.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
