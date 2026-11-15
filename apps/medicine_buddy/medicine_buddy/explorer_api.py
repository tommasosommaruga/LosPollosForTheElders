"""Auth-gated explorer JSON handlers used by /medicine/api/* (session required by web_ui).

All responses are built from real SQLite data. Never includes passphrase material or
utterance text. Person filters use exact id or exact name_key only (no fuzzy merge).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from medicine_buddy.db import MEMORY_KINDS, ActivityEvent, Database, public_person

_FORBIDDEN_KEYS = frozenset({
    "pass_salt", "pass_hash", "pass_words", "pass_iterations",
    "secret", "secret_hash", "passphrase", "digest", "salt",
})


def assert_safe_payload(obj: Any) -> None:
    """Raise AssertionError if a response tree contains forbidden secret keys."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _FORBIDDEN_KEYS or (isinstance(key, str) and key.startswith("pass_")):
                raise AssertionError(f"forbidden key in explorer payload: {key}")
            assert_safe_payload(value)
    elif isinstance(obj, list):
        for item in obj:
            assert_safe_payload(item)


def _activity_dict(event: ActivityEvent, person_name: str | None = None) -> dict[str, Any]:
    return {
        "id": event.id,
        "at": datetime.fromtimestamp(event.at_ts).isoformat(timespec="seconds"),
        "at_ts": event.at_ts,
        "via": event.via,
        "method": event.method,
        "path": event.path,
        "status": event.status,
        "ms": event.ms,
        "result": event.result,
        "error": event.error,
        "blocked": event.blocked,
        "final_state": event.final_state,
        "verified": event.verified,
        "speak_hint": event.speak_hint,
        "utterance_chars": event.utterance_chars,
        "person_id": event.person_id,
        "person_name": person_name,
    }


def _resolve_person_filter(db: Database, person: str | None) -> tuple[int | None, str | None]:
    """Exact person id or exact name_key. Returns (person_id, error)."""
    if not person or not str(person).strip():
        return None, None
    raw = str(person).strip()
    if raw.isdigit():
        pid = int(raw)
        if db.get_person(pid) is None:
            return None, f"unknown person id={pid}"
        return pid, None
    found = db.get_person_by_name(raw)
    if found is None:
        return None, f"unknown person {raw!r} (exact name match only)"
    return found.id, None


def _person_names(db: Database) -> dict[int, str]:
    return {p.id: p.name for p in db.list_people()}


def activity_response(db: Database, params: dict[str, Any]) -> dict[str, Any]:
    person_id, err = _resolve_person_filter(db, params.get("person"))
    if err:
        return {"error": err}
    since_id = _int_or_none(params.get("since_id"))
    status_class = (params.get("status_class") or "").strip().lower()
    status_min = status_max = None
    if status_class == "ok":
        status_min, status_max = 200, 399
    elif status_class == "error":
        status_min = 400
    via = (params.get("via") or "").strip() or None
    path_prefix = (params.get("path") or "").strip() or None
    since_ts = _float_or_none(params.get("from_ts")) or _iso_to_ts(params.get("from"))
    until_ts = _float_or_none(params.get("to_ts")) or _iso_to_ts(params.get("to"))
    limit = _int_or_none(params.get("limit")) or 50
    offset = _int_or_none(params.get("offset")) or 0
    filters = dict(
        since_id=since_id, via=via, status_min=status_min, status_max=status_max,
        path_prefix=path_prefix, person_id=person_id, since_ts=since_ts, until_ts=until_ts,
    )
    names = _person_names(db)
    items = db.query_activity_events(**filters, limit=limit, offset=offset)
    total = db.count_activity_events(**filters)
    payload = {
        "items": [_activity_dict(e, names.get(e.person_id) if e.person_id else None) for e in items],
        "total": total,
        "limit": limit,
        "offset": offset,
        "since_id": since_id,
        "next_since_id": items[-1].id if items else since_id,
    }
    assert_safe_payload(payload)
    return payload


def search_response(db: Database, params: dict[str, Any]) -> dict[str, Any]:
    person_id, err = _resolve_person_filter(db, params.get("person"))
    if err:
        return {"error": err}
    q = (params.get("q") or "").strip()
    kind = (params.get("kind") or "").strip() or None
    since = (params.get("from") or "").strip() or None
    until = (params.get("to") or "").strip() or None
    limit = _int_or_none(params.get("limit")) or 30
    offset = _int_or_none(params.get("offset")) or 0
    results: list[dict[str, Any]] = []
    names = _person_names(db)

    include_people = kind in (None, "person")
    include_memory = kind in (None, "memory") or (kind in MEMORY_KINDS)
    include_dose = kind in (None, "dose")
    include_medicine = kind in (None, "medicine")
    include_activity = kind in (None, "activity")

    if include_people and q and person_id is None:
        for p in db.search_people(q):
            results.append({
                "kind": "person", "id": p.id, "title": p.name, "person_id": p.id,
                "person_name": p.name, "role": p.role, "snippet": p.role,
            })

    if include_memory:
        mem_kind = kind if kind in MEMORY_KINDS else None
        for m in db.search_memories(
            q or None, person_id=person_id, kind=mem_kind,
            since=since, until=until, limit=limit, offset=0,
        ):
            text = m["text"]
            results.append({
                "kind": "memory",
                "id": m["id"],
                "title": m["title"],
                "person_id": m["person_id"],
                "person_name": m["person_name"],
                "memory_kind": m["kind"],
                "snippet": (text[:160] + "…") if len(text) > 160 else text,
                "created_at": m["created_at"],
            })

    if include_dose:
        doses = db.query_dose_events(
            person_id=person_id, since=since, until=until, limit=limit, offset=0,
        )
        if q:
            ql = q.casefold()
            doses = [
                d for d in doses
                if ql in (d["medicine_name"] or "").casefold()
                or ql in (d["state"] or "").casefold()
                or ql in (d["detail"] or "").casefold()
                or ql in (d["person_name"] or "").casefold()
            ]
        for d in doses:
            results.append({
                "kind": "dose",
                "id": d["id"],
                "title": f"{d['medicine_name']} → {d['state']}",
                "person_id": d["person_id"],
                "person_name": d["person_name"],
                "snippet": d["detail"] or d["created_at"],
                "created_at": d["created_at"],
                "state": d["state"],
                "medicine_id": d["medicine_id"],
            })

    if include_medicine:
        meds = (
            db.list_medicines(person_id, all_people=False)
            if person_id is not None
            else db.list_medicines()
        )
        if q:
            ql = q.casefold()
            meds = [m for m in meds if ql in m.name.casefold()]
        for m in meds:
            results.append({
                "kind": "medicine",
                "id": m.id,
                "title": m.name,
                "person_id": m.person_id,
                "person_name": names.get(m.person_id) if m.person_id else None,
                "snippet": "medicine",
            })

    if include_activity and q:
        acts = db.query_activity_events(person_id=person_id, limit=200, offset=0)
        ql = q.casefold()
        for e in acts:
            blob = " ".join(
                str(x) for x in (e.path, e.result, e.error, e.final_state, e.via) if x
            ).casefold()
            if ql in blob:
                results.append({
                    "kind": "activity",
                    "id": e.id,
                    "title": f"{e.method} {e.path}",
                    "person_id": e.person_id,
                    "person_name": names.get(e.person_id) if e.person_id else None,
                    "snippet": e.error or e.result or e.final_state or str(e.status),
                    "created_at": datetime.fromtimestamp(e.at_ts).isoformat(timespec="seconds"),
                })

    total = len(results)
    page = results[offset: offset + limit]
    payload = {"items": page, "total": total, "limit": limit, "offset": offset, "q": q}
    assert_safe_payload(payload)
    return payload


def people_list_response(db: Database) -> dict[str, Any]:
    items = []
    for p in db.list_people():
        meds = db.list_medicines(p.id, all_people=False)
        items.append({
            **public_person(p),
            "medicines": len(meds),
            "memories": db.count_memories_search(person_id=p.id),
            "dose_events": db.count_dose_events_query(person_id=p.id),
        })
    payload = {"items": items, "total": len(items)}
    assert_safe_payload(payload)
    return payload


def person_records_response(db: Database, person_id: int) -> dict[str, Any]:
    dossier = db.get_person_records(person_id)
    if dossier is None:
        return {"error": f"no person with id={person_id}"}
    assert_safe_payload(dossier)
    return dossier


def record_detail_response(db: Database, kind: str, record_id: int) -> dict[str, Any]:
    kind = kind.strip().lower()
    names = _person_names(db)
    if kind == "memory":
        m = db.get_memory(record_id)
        if m is None:
            return {"error": f"no memory with id={record_id}"}
        person = db.get_person(m.person_id)
        payload = {
            "kind": "memory",
            "id": m.id,
            "person_id": m.person_id,
            "person_name": person.name if person else None,
            "memory_kind": m.kind,
            "title": m.title,
            "text": m.text,
            "created_at": m.created_at,
        }
        assert_safe_payload(payload)
        return payload
    if kind in {"dose", "dose_event"}:
        row = db.get_dose_event_detail(record_id)
        if row is None:
            return {"error": f"no dose event with id={record_id}"}
        timeline = db.list_dose_events(medicine_id=row["medicine_id"], limit=40)
        payload = {
            "kind": "dose_event",
            **row,
            "timeline": [
                {
                    "id": e.id, "state": e.state.value, "detail": e.detail,
                    "created_at": e.created_at, "slot_id": e.slot_id,
                }
                for e in timeline
            ],
        }
        assert_safe_payload(payload)
        return payload
    if kind == "activity":
        e = db.get_activity_event(record_id)
        if e is None:
            return {"error": f"no activity with id={record_id}"}
        payload = _activity_dict(e, names.get(e.person_id) if e.person_id else None)
        payload["kind"] = "activity"
        assert_safe_payload(payload)
        return payload
    if kind == "medicine":
        m = db.get_medicine(record_id)
        if m is None:
            return {"error": f"no medicine with id={record_id}"}
        schedules = [
            {
                "id": s.id, "weekdays": s.weekdays, "time_hhmm": s.time_hhmm,
                "timezone": s.timezone, "active": s.active,
            }
            for s in db.list_schedules_for_medicine(m.id, active_only=False)
        ]
        payload = {
            "kind": "medicine",
            "id": m.id,
            "name": m.name,
            "person_id": m.person_id,
            "person_name": names.get(m.person_id) if m.person_id else None,
            "schedules": schedules,
            "recent_doses": db.query_dose_events(medicine_id=m.id, limit=20),
        }
        assert_safe_payload(payload)
        return payload
    if kind == "person":
        dossier = db.get_person_records(record_id)
        if dossier is None:
            return {"error": f"no person with id={record_id}"}
        dossier = {**dossier, "kind": "person"}
        assert_safe_payload(dossier)
        return dossier
    return {"error": f"unknown record kind {kind!r}"}


def adherence_response(db: Database, params: dict[str, Any]) -> dict[str, Any]:
    person_id, err = _resolve_person_filter(db, params.get("person"))
    if err:
        return {"error": err}
    days = _int_or_none(params.get("days")) or 7
    report = db.adherence_report(person_id=person_id, days=days)
    if person_id is not None:
        p = db.get_person(person_id)
        report["person_name"] = p.name if p else None
    assert_safe_payload(report)
    return report


def agent_status_response(service: Any) -> dict[str, Any]:
    snap = service.debug_snapshot()
    payload = {
        "now": snap.get("now"),
        "timezone": snap.get("timezone"),
        "uptime_s": snap.get("uptime_s"),
        "debug": snap.get("debug"),
        "reminders": snap.get("reminders"),
        "app": {
            "versions": (snap.get("app") or {}).get("versions"),
            "dry_run": (snap.get("app") or {}).get("dry_run"),
            "python": (snap.get("app") or {}).get("python"),
            "platform": (snap.get("app") or {}).get("platform"),
        },
        "voice_guard": {
            "enabled": snap["voice_guard"]["enabled"],
            "utterances_heard": snap["voice_guard"]["utterances_heard"],
            "seconds_since_last_speech": snap["voice_guard"]["seconds_since_last_speech"],
            "unlocked_people": snap["voice_guard"]["unlocked_people"],
            "open_questions": snap["voice_guard"]["open_questions"],
            "blocked_count": len(snap["voice_guard"].get("blocked") or []),
        },
        "data": snap.get("data"),
    }
    assert_safe_payload(payload)
    return payload


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_to_ts(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if len(text) == 10 and text[4] == "-":
            text = text + "T00:00:00+00:00"
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
