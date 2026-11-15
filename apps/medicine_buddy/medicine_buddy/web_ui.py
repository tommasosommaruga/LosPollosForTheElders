"""Medicine Buddy pages inside the Reachy Mini app's web UI (the page Reachy Mini Control opens).

Mounted under /medicine on the app's settings server (port 7860, next to Pollen's
conversation settings at /):
    /medicine          dashboard (people, schedules, due now, memories, dose log, security)
    /medicine/history  activity timeline, search, people explorer, adherence reports
    /medicine/people/{id}  per-person dossier
    /medicine/records/{kind}/{id}  record detail
    /medicine/api/*    auth-gated JSON for the explorer (session cookie)
    /medicine/family   passphrases and people (typed, never spoken)
    /medicine/debug    live debug view + the debug on/off switch

That server listens on the whole network (so Reachy Mini Control can open it), unlike the
:8787 API which is local-only. Therefore everything here needs a LOGIN:
- the caregiver's passphrase (any registered person's while there is no caregiver yet;
  open while nobody is registered at all - first-time setup),
- at most 5 wrong attempts per device per 5 minutes,
- session cookie: random, HttpOnly, SameSite=Strict, 30 min; every change also needs the
  per-session token in X-Family-Token, and cross-site requests are refused.
Known limit: the page is plain HTTP on the local network, so use it on a trusted network.
"""

# No `from __future__ import annotations` here: FastAPI must see the real `Request` type
# on the route functions, otherwise it treats `request` as a query parameter (HTTP 422).
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from medicine_buddy import explorer_api
from medicine_buddy.family_page import render_family_page
from medicine_buddy.history_page import render_history_page, render_person_page, render_record_page

PREFIX = "/medicine"
COOKIE = "mb_session"
SESSION_SECONDS = 30 * 60
MAX_FAILURES = 5
FAILURE_WINDOW = 5 * 60


class _Sessions:
    def __init__(self) -> None:
        self._s: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, list[float]] = {}

    def create(self, who: str) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        self._s[token] = {"who": who, "csrf": secrets.token_urlsafe(24), "expires": time.time() + SESSION_SECONDS}
        return token, self._s[token]

    def get(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        s = self._s.get(token)
        if s is None or s["expires"] < time.time():
            self._s.pop(token, None)
            return None
        s["expires"] = time.time() + SESSION_SECONDS  # sliding expiry while in use
        return s

    def drop(self, token: str | None) -> None:
        if token:
            self._s.pop(token, None)

    def locked_for(self, client: str) -> int:
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW]
        self._failures[client] = recent
        if len(recent) >= MAX_FAILURES:
            return int(FAILURE_WINDOW - (now - recent[0])) + 1
        return 0

    def failed(self, client: str) -> None:
        self._failures.setdefault(client, []).append(time.time())


def _nav(active: str, who: str) -> str:
    def link(href: str, label: str, key: str) -> str:
        style = "font-weight:700;text-decoration:underline" if key == active else ""
        return f'<a href="{href}" style="margin-right:14px;{style}">{label}</a>'
    who_html = (who.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (
        '<div style="font-size:14px;font-weight:500;margin:6px 0 2px">'
        + link(PREFIX, "Dashboard", "dash")
        + link(f"{PREFIX}/conversation", "Conversation", "conv")
        + link(f"{PREFIX}/data", "Database", "data")
        + link(f"{PREFIX}/history", "History", "history")
        + link(f"{PREFIX}/family", "Family &amp; passphrases", "family")
        + link(f"{PREFIX}/debug", "Debug", "debug")
        + link("/", "Voice &amp; language settings", "voice")
        + f'<span style="color:var(--muted,#888);margin-left:6px">signed in: {who_html}</span> '
        + f'<a href="{PREFIX}/logout" style="margin-left:8px">Log out</a></div>'
    )


class _Starting(Exception):
    pass


class _LazyService:
    """The web server starts before the app's run(); until the service exists, pages show
    'starting...' instead of 404."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        target = self._provider() if callable(self._provider) else self._provider
        if target is None:
            raise _Starting()
        return getattr(target, name)


def attach_web_ui(app: Any, service_or_provider: Any) -> None:
    """Add the /medicine routes to the app's FastAPI settings server.

    Pass the service, or a zero-argument callable returning it (None while starting)."""
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

    service = _LazyService(service_or_provider)
    sessions = _Sessions()
    app.state.medicine_sessions = sessions  # for tests

    @app.exception_handler(_Starting)
    async def _starting(request: Request, exc: _Starting):
        if request.method == "GET" and not request.url.path.endswith("/data"):
            return HTMLResponse(_STARTING_PAGE, status_code=503, headers={"Retry-After": "2", "Cache-Control": "no-store"})
        return JSONResponse({"error": "Medicine Buddy is still starting"}, 503)

    def headers() -> dict[str, str]:
        return {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}

    def html(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(body, status_code=status, headers=headers())

    def client_id(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def cross_site(request: Request) -> bool:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return True
        origin = request.headers.get("origin")
        return origin is not None and urlparse(origin).netloc.lower() != request.headers.get("host", "").lower()

    def open_setup_mode() -> bool:
        return not service.db.list_people()

    def session_for(request: Request) -> dict[str, Any] | None:
        s = sessions.get(request.cookies.get(COOKIE))
        if s is None and open_setup_mode():
            return {"who": "setup (nobody registered yet)", "csrf": "", "open": True}
        return s

    def track(request: Request, status: int, start: float, payload: dict | None = None) -> None:
        service.record_request(request.method, request.url.path, status,
                               (time.perf_counter() - start) * 1000, payload, via="app-ui")

    def need_change_rights(request: Request) -> tuple[dict | None, JSONResponse | None]:
        if cross_site(request):
            return None, JSONResponse({"error": "requests from other websites are not allowed"}, 403)
        s = session_for(request)
        if s is None:
            return None, JSONResponse({"error": "please sign in again"}, 401)
        if not s.get("open") and not hmac.compare_digest(
                request.headers.get("x-family-token", ""), s["csrf"]):
            return None, JSONResponse({"error": "invalid page token - reload the page"}, 403)
        return s, None

    # ------------------------------------------------------------------ login
    @app.get(PREFIX)
    def dashboard(request: Request):
        start = time.perf_counter()
        s = session_for(request)
        if s is None:
            return html(_login_page())
        track(request, 200, start)
        return html(service.status_page(nav=_nav("dash", s["who"])))

    @app.post(f"{PREFIX}/login")
    async def login(request: Request):
        start = time.perf_counter()
        if cross_site(request):
            return JSONResponse({"error": "requests from other websites are not allowed"}, 403)
        client = client_id(request)
        wait = sessions.locked_for(client)
        if wait:
            track(request, 429, start, {"error": "too many attempts"})
            return JSONResponse({"error": f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes."}, 429)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        phrase = str((body or {}).get("passphrase") or "")
        people = service.db.list_people()
        caregivers = [p for p in people if p.is_caregiver]
        allowed = caregivers or people
        who = next((p for p in allowed if phrase and p.secret.matches(phrase)), None)
        if who is None:
            sessions.failed(client)
            service.guard.refuse("web login", f"wrong passphrase from {client}", "")
            track(request, 401, start, {"error": "wrong passphrase"})
            hint = "the caregiver's passphrase" if caregivers else "a family member's passphrase"
            return JSONResponse({"error": f"That is not {hint}."}, 401)
        token, _ = sessions.create(f"{who.name} ({'caregiver' if who.is_caregiver else 'family member'})")
        track(request, 200, start, {"status": "ok"})
        resp = JSONResponse({"status": "ok"})
        resp.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path=PREFIX)
        return resp

    @app.get(f"{PREFIX}/logout")
    def logout(request: Request):
        sessions.drop(request.cookies.get(COOKIE))
        resp = RedirectResponse(PREFIX, status_code=303)
        resp.delete_cookie(COOKIE, path=PREFIX)
        return resp

    # ------------------------------------------------------------------ family
    @app.get(f"{PREFIX}/family")
    def family(request: Request):
        s = session_for(request)
        if s is None:
            return RedirectResponse(PREFIX, status_code=303)
        return html(render_family_page(s["csrf"], base=PREFIX, nav=_nav("family", s["who"])))

    @app.get(PREFIX + "/calendar/{token}.ics")
    def calendar_feed(token: str):
        """Subscribe URL for Outlook / Google / Apple — token is the secret (no session)."""
        ics = service.export_ics_for_token(token)
        if ics is None:
            return JSONResponse({"error": "unknown or revoked calendar link"}, 404)
        return Response(
            content=ics,
            media_type="text/calendar; charset=utf-8",
            headers={**headers(), "Content-Disposition": "inline; filename=medicine-buddy.ics"},
        )

    # resolved per request (the service may not exist yet while the app starts)
    settings_routes = {
        "people": "settings_people",
        "suggest": "settings_suggest",
        "register": "settings_register",
        "passphrase": "settings_change_passphrase",
        "timezone": "settings_timezone",
        "require_passphrases": "settings_require_passphrases",
        "caregiver_alerts": "settings_caregiver_alerts",
        "encourage_self_care": "settings_encourage_self_care",
        "family_call": "settings_family_call",
        "checkins": "settings_checkins",
        "activity_nudges": "settings_activity_nudges",
        "calendar": "settings_calendar",
        "calendar_accept": "accept_calendar_proposal",
        "calendar_reject": "reject_calendar_proposal",
    }

    @app.post(PREFIX + "/settings/{name}")
    async def settings(name: str, request: Request):
        start = time.perf_counter()
        _, denied = need_change_rights(request)
        if denied is not None:
            track(request, denied.status_code, start)
            return denied
        route = settings_routes.get(name)
        if route is None:
            return JSONResponse({"error": "unknown setting"}, 404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "JSON expected"}, 400)
        payload = getattr(service, route)(body if isinstance(body, dict) else {})
        status = 400 if "error" in payload else 200
        track(request, status, start, payload)
        return JSONResponse(payload, status)

    # ------------------------------------------------------------------ conversation + database
    @app.get(f"{PREFIX}/conversation")
    def conversation_page(request: Request):
        s = session_for(request)
        if s is None:
            return RedirectResponse(PREFIX, status_code=303)
        return html(_conversation_page(s["csrf"], _nav("conv", s["who"])))

    @app.get(f"{PREFIX}/conversation/data")
    def conversation_data(request: Request, after: int = 0):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return JSONResponse(service.transcript_snapshot(after), headers=headers())

    @app.post(f"{PREFIX}/conversation/clear")
    async def conversation_clear(request: Request):
        _, denied = need_change_rights(request)
        if denied is not None:
            return denied
        return JSONResponse(service.clear_transcript())

    @app.get(f"{PREFIX}/data")
    def database_page(request: Request):
        s = session_for(request)
        if s is None:
            return RedirectResponse(PREFIX, status_code=303)
        return html(_database_page(_nav("data", s["who"])))

    @app.get(f"{PREFIX}/data/tables")
    def database_data(request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return JSONResponse(service.database_snapshot(), headers=headers())
    # ------------------------------------------------------------------ history / explorer
    @app.get(f"{PREFIX}/history")
    def history_page(request: Request):
        s = session_for(request)
        if s is None:
            return RedirectResponse(PREFIX, status_code=303)
        return html(render_history_page(
            s["csrf"], _nav("history", s["who"]), base_css=_BASE_CSS, prefix=PREFIX,
        ))

    @app.get(PREFIX + "/people/{person_id}")
    def person_page(person_id: int, request: Request):
        s = session_for(request)
        if s is None:
            return RedirectResponse(PREFIX, status_code=303)
        dossier = explorer_api.person_records_response(service.db, person_id)
        status = 404 if "error" in dossier else 200
        return html(
            render_person_page(
                s["csrf"], _nav("history", s["who"]), person_id, dossier,
                base_css=_BASE_CSS, prefix=PREFIX,
            ),
            status=status,
        )

    @app.get(PREFIX + "/records/{kind}/{record_id}")
    def record_page(kind: str, record_id: int, request: Request):
        s = session_for(request)
        if s is None:
            return RedirectResponse(PREFIX, status_code=303)
        record = explorer_api.record_detail_response(service.db, kind, record_id)
        status = 404 if "error" in record else 200
        return html(
            render_record_page(
                s["csrf"], _nav("history", s["who"]), kind, record,
                base_css=_BASE_CSS, prefix=PREFIX,
            ),
            status=status,
        )

    def _query_params(request: Request) -> dict[str, Any]:
        return {k: v for k, v in request.query_params.multi_items()}

    def _api_json(payload: dict[str, Any]) -> JSONResponse:
        if "error" in payload:
            err = str(payload["error"])
            status = 404 if err.startswith("no ") else 400
        else:
            status = 200
        return JSONResponse(payload, status_code=status, headers=headers())

    @app.get(f"{PREFIX}/api/activity")
    def api_activity(request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return _api_json(explorer_api.activity_response(service.db, _query_params(request)))

    @app.get(f"{PREFIX}/api/search")
    def api_search(request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return _api_json(explorer_api.search_response(service.db, _query_params(request)))

    @app.get(f"{PREFIX}/api/people")
    def api_people(request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return _api_json(explorer_api.people_list_response(service.db))

    @app.get(PREFIX + "/api/people/{person_id}/records")
    def api_person_records(person_id: int, request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return _api_json(explorer_api.person_records_response(service.db, person_id))

    @app.get(PREFIX + "/api/records/{kind}/{record_id}")
    def api_record_detail(kind: str, record_id: int, request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return _api_json(explorer_api.record_detail_response(service.db, kind, record_id))

    @app.get(f"{PREFIX}/api/reports/adherence")
    def api_adherence(request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return _api_json(explorer_api.adherence_response(service.db, _query_params(request)))

    @app.get(f"{PREFIX}/api/agent")
    def api_agent(request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return _api_json(explorer_api.agent_status_response(service))

    # ------------------------------------------------------------------ debug
    @app.get(f"{PREFIX}/debug")
    def debug_page(request: Request):
        s = session_for(request)
        if s is None:
            return RedirectResponse(PREFIX, status_code=303)
        return html(_debug_page(s["csrf"], _nav("debug", s["who"])))

    @app.get(f"{PREFIX}/debug/data")
    def debug_data(request: Request):
        if session_for(request) is None:
            return JSONResponse({"error": "please sign in again"}, 401)
        return JSONResponse(service.debug_snapshot(), headers=headers())

    @app.post(f"{PREFIX}/debug/toggle")
    async def debug_toggle(request: Request):
        start = time.perf_counter()
        _, denied = need_change_rights(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        payload = service.set_debug(bool((body or {}).get("enabled")))
        track(request, 200, start, {"status": "debug on" if payload["debug"] else "debug off"})
        return JSONResponse(payload)


# ---------------------------------------------------------------------- pages

_BASE_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#77756f;--line:#e4e2dc;--accent:#2f6f4f;--warn:#c0392b;--soft:#eaf3ee}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--muted:#9a978f;--line:#33322e;--accent:#7cc39c;--warn:#ef8a78;--soft:#1d2a22}}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px} a{color:var(--accent)}
h1{font-size:22px;margin:0 0 4px} h2{font-size:14px;margin:0 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px;overflow-x:auto}
table{width:100%;border-collapse:collapse} td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}
th{font-weight:600;color:var(--muted);font-size:13px} tr:last-child td{border-bottom:0}
.muted{color:var(--muted)} .ok{color:var(--accent)} .bad{color:var(--warn)}
input,button{font:inherit;border-radius:10px;border:1px solid var(--line);padding:11px 12px;background:var(--bg);color:var(--fg)}
button{cursor:pointer;background:var(--card)} button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.kv div{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px solid var(--line);font-size:14px}
.kv div:last-child{border-bottom:0} .kv span:first-child{color:var(--muted)}
code{font-size:13px;word-break:break-all}
.switch{display:inline-flex;align-items:center;gap:10px;font-weight:600;cursor:pointer;user-select:none}
.switch input{appearance:none;width:46px;height:26px;border-radius:99px;background:var(--line);position:relative;padding:0;border:0;cursor:pointer}
.switch input::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:left .15s}
.switch input:checked{background:var(--accent)} .switch input:checked::after{left:23px}
"""


_STARTING_PAGE = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><meta http-equiv=refresh content=2>
<title>Medicine Buddy - starting</title><style>{_BASE_CSS}</style></head><body><main>
<h1>Medicine Buddy is starting&hellip;</h1><p class=muted>This page reloads by itself in a moment.</p>
</main></body></html>"""


def _login_page() -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Medicine Buddy - sign in</title>
<style>{_BASE_CSS} .box{{max-width:420px;margin:12vh auto}}</style></head><body><main><div class="box">
<h1>Medicine Buddy</h1><p class=muted>Sign in with the <b>caregiver's passphrase</b> to see the dashboard,
history explorer, family settings and debug tools.</p>
<section><form id=f><input id=p type=password autocomplete=current-password placeholder="Caregiver passphrase"
style="width:100%;margin-bottom:10px" autofocus><button class=primary style="width:100%">Sign in</button></form>
<p id=m class=bad></p></section>
<p class=muted style="font-size:13px">Typed here, the passphrase never passes through Reachy's AI.
<a href="/">Voice &amp; language settings</a></p></div></main>
<script>
document.getElementById("f").addEventListener("submit", async e => {{
  e.preventDefault();
  const r = await fetch({json.dumps(PREFIX + "/login")}, {{method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{passphrase: document.getElementById("p").value}})}});
  if (r.ok) location.reload();
  else {{ const d = await r.json().catch(() => ({{}})); document.getElementById("m").textContent = d.error || "Sign-in failed"; }}
}});
</script></body></html>"""


_COMMON_JS = """
const $ = id => document.getElementById(id);
function td(text, cls) { const c = document.createElement("td"); c.textContent = text == null ? "" : String(text); if (cls) c.className = cls; return c; }
"""


def _conversation_page(csrf: str, nav: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Medicine Buddy - conversation</title>
<style>{_BASE_CSS}
#chat{{display:flex;flex-direction:column;gap:8px;max-height:62vh;overflow-y:auto;padding:4px}}
.msg{{max-width:80%;padding:9px 13px;border-radius:14px;white-space:pre-wrap;word-break:break-word}}
.msg small{{display:block;color:var(--muted);font-size:11px;margin-bottom:2px}}
.reachy{{align-self:flex-start;background:var(--soft);border:1px solid var(--line)}}
.person{{align-self:flex-end;background:var(--card);border:1px solid var(--line)}}
</style></head><body><main>
<h1>Conversation</h1>{nav}
<p class=muted>Live: what the person said and what Reachy answered. Kept in memory only (last 200 lines),
never saved to the database or logs, and gone when the app restarts. Spoken passphrases are hidden.</p>
<section>
  <div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:10px;flex-wrap:wrap">
    <label class=switch><input type=checkbox id=follow checked> <span>Follow newest</span></label>
    <button id=clear>Clear</button></div>
  <div id=chat><p class=muted id=empty>Nothing said yet. Say &ldquo;Hey Reachy&rdquo;.</p></div>
</section>
<script>
const TOKEN = {json.dumps(csrf)}; const P = {json.dumps(PREFIX)};
{_COMMON_JS}
let last = 0; const seen = new Map();
async function poll() {{
  const r = await fetch(P + "/conversation/data?after=0", {{cache: "no-store"}});
  if (r.status === 401) {{ location.href = P; return; }}
  const d = await r.json(); const chat = $("chat");
  if (d.last_id < last) {{ chat.replaceChildren(); seen.clear(); }}
  last = d.last_id;
  for (const l of d.lines) {{
    let el = seen.get(l.id);
    if (!el) {{ el = document.createElement("div"); el.className = "msg " + l.who;
      el.append(document.createElement("small"), document.createElement("span")); seen.set(l.id, el); chat.append(el); }}
    el.firstChild.textContent = (l.who === "reachy" ? "Reachy" : "Person") + " - " + l.time;
    el.lastChild.textContent = l.text;
  }}
  if (d.lines.length) {{ const e = $("empty"); if (e) e.remove(); if ($("follow").checked) chat.scrollTop = chat.scrollHeight; }}
}}
$("clear").addEventListener("click", async () => {{
  await fetch(P + "/conversation/clear", {{method: "POST", headers: {{"X-Family-Token": TOKEN}}}}); seen.clear(); $("chat").replaceChildren(); last = 0; poll(); }});
poll(); setInterval(poll, 2000);
</script></main></body></html>"""


def _database_page(nav: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Medicine Buddy - database</title>
<style>{_BASE_CSS}
.tabs button{{margin:0 6px 6px 0}} .tabs button.on{{background:var(--accent);color:#fff;border-color:var(--accent)}}
td{{white-space:pre-wrap;word-break:break-word}}
</style></head><body><main>
<h1>Database</h1>{nav}
<p class=muted>Read-only view of everything stored on this machine. Changes are made by voice or on the Family page.
Passphrases are stored only as salted hashes and are never shown.</p>
<section><div class=kv id=info></div></section>
<section><div class=tabs id=tabs></div><p class=muted id=note></p>
<table><thead id=head></thead><tbody id=body></tbody></table></section>
<script>
const P = {json.dumps(PREFIX)};
{_COMMON_JS}
let data = null, cur = "medicines";
try {{ cur = localStorage.getItem("mb_table") || cur; }} catch (e) {{}}
function show() {{
  const t = data.tables.find(x => x.name === cur) || data.tables[0]; cur = t.name;
  $("tabs").replaceChildren(...data.tables.map(x => {{ const b = document.createElement("button");
    b.textContent = x.name + " (" + x.rows.length + ")"; if (x.name === cur) b.className = "on";
    b.onclick = () => {{ cur = x.name; try {{ localStorage.setItem("mb_table", cur); }} catch (e) {{}} show(); }}; return b; }}));
  $("note").textContent = t.note || "";
  const hr = document.createElement("tr"); t.columns.forEach(c => {{ const h = document.createElement("th"); h.textContent = c; hr.append(h); }});
  $("head").replaceChildren(hr);
  if (!t.rows.length) {{ const tr = document.createElement("tr"); const c = td("Empty.", "muted"); c.colSpan = t.columns.length; tr.append(c); $("body").replaceChildren(tr); return; }}
  $("body").replaceChildren(...t.rows.map(r => {{ const tr = document.createElement("tr"); r.forEach(v => tr.append(td(v))); return tr; }}));
}}
async function load() {{
  const r = await fetch(P + "/data/tables", {{cache: "no-store"}});
  if (r.status === 401) {{ location.href = P; return; }}
  data = await r.json();
  $("info").replaceChildren(...[["file", data.path], ["size", data.size_bytes == null ? "-" : Math.round(data.size_bytes / 1024) + " KB"]].map(([k, v]) => {{
    const d = document.createElement("div"), a = document.createElement("span"), b = document.createElement("span"); a.textContent = k; b.textContent = v; d.append(a, b); return d; }}));
  show();
}}
load(); setInterval(load, 5000);
</script></main></body></html>"""


def _debug_page(csrf: str, nav: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Medicine Buddy - debug</title>
<style>{_BASE_CSS}</style></head><body><main>
<h1>Debug</h1>{nav}
<p class=muted>Live view, refreshes every 3 seconds. Never shows what people said or any passphrase.</p>
<section style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
  <label class=switch><input type=checkbox id=dbg> <span>Debug mode</span></label>
  <span class=muted id=dbgnote>When on: detailed Medicine Buddy logs (DEBUG level) in the app's log output.</span>
</section>
<div class=grid>
  <section><h2>App</h2><div class=kv id=app></div></section>
  <section><h2>Voice guard</h2><div class=kv id=guard></div></section>
  <section><h2>Data</h2><div class=kv id=data></div></section>
  <section><h2>Reminders</h2><div class=kv id=rem></div></section>
</div>
<section><h2>Unlocked by spoken passphrase</h2><table><thead><tr><th>Person</th><th>Still valid</th></tr></thead><tbody id=unlocked></tbody></table></section>
<section><h2>Open confirmation questions</h2><table><thead><tr><th>Action</th><th>Expires in</th></tr></thead><tbody id=pending></tbody></table></section>
<section><h2>Blocked by the voice guard / login</h2><table><thead><tr><th>When</th><th>Action</th><th>Why</th></tr></thead><tbody id=blocked></tbody></table></section>
<section><h2>Recent requests (tools, pages, settings)</h2><table><thead><tr><th>When</th><th>From</th><th>Request</th><th>Status</th><th>ms</th><th>Outcome</th></tr></thead><tbody id=reqs></tbody></table></section>
<script>
const TOKEN = {json.dumps(csrf)};
const P = {json.dumps(PREFIX)};
const $ = id => document.getElementById(id);
function td(text, cls) {{ const c = document.createElement("td"); c.textContent = text == null ? "" : String(text); if (cls) c.className = cls; return c; }}
function rows(id, list, cols, empty) {{
  const body = $(id);
  if (!list.length) {{ const tr = document.createElement("tr"); const c = td(empty, "muted"); c.colSpan = cols; tr.append(c); body.replaceChildren(tr); return; }}
  body.replaceChildren(...list.map(r => {{ const tr = document.createElement("tr"); tr.append(...r); return tr; }}));
}}
function kv(id, obj) {{
  $(id).replaceChildren(...Object.entries(obj).map(([k, v]) => {{
    const d = document.createElement("div"); const a = document.createElement("span"); const b = document.createElement("span");
    a.textContent = k.replaceAll("_", " "); b.textContent = (v === null || v === undefined) ? "-" : (typeof v === "object" ? JSON.stringify(v) : String(v));
    d.append(a, b); return d; }}));
}}
function mmss(s) {{ return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }}
async function refresh() {{
  const r = await fetch(P + "/debug/data", {{cache: "no-store"}});
  if (r.status === 401) {{ location.href = P; return; }}
  const d = await r.json();
  $("dbg").checked = d.debug;
  kv("app", {{uptime: mmss(d.uptime_s), ...d.app.versions, python: d.app.python, dry_run: d.app.dry_run,
              database: d.app.database, catalog_medicines: d.app.catalog_size}});
  kv("guard", {{enabled: d.voice_guard.enabled, utterances_heard: d.voice_guard.utterances_heard,
                last_speech: d.voice_guard.seconds_since_last_speech == null ? "never" : d.voice_guard.seconds_since_last_speech + " s ago"}});
  kv("data", d.data);
  const rm = d.reminders || {{}};
  kv("rem", {{running: !!rm.running, check_every: rm.check_every_s ? rm.check_every_s + " s" : null, last_spoken: rm.last_spoken,
              last_error: rm.last_error, reachy: rm.conversation ? (rm.conversation.watching ? (rm.conversation.state || "idle") + (rm.conversation.pending_tools ? " (" + rm.conversation.pending_tools + " tool call)" : "") : "not watching") : null, waiting_for_answer: (rm.waiting_for_answer || []).map(w => w.medicine + (w.person ? " (" + w.person + ")" : "") + " x" + w.reminders_spoken).join(", ") || "-"}});
  rows("unlocked", d.voice_guard.unlocked_people.map(u => [td(u.name), td(mmss(u.seconds_left))]), 2, "Nobody unlocked right now.");
  rows("pending", d.voice_guard.open_questions.map(q => [td(q.action), td(q.seconds_left + " s")]), 2, "No open questions.");
  rows("blocked", d.voice_guard.blocked.map(b => [td(b.at, "muted"), td(b.action), td(b.reason)]), 3, "Nothing blocked.");
  rows("reqs", d.recent_requests.map(q => [td(q.at, "muted"), td(q.via), td(q.method + " " + q.path),
      td(q.status, q.status >= 400 ? "bad" : "ok"), td(q.ms),
      td([q.blocked ? "BLOCKED" : "", q.error || "", q.final_state || "", q.result || "", q.speak_hint || ""].filter(Boolean).join(" | "))]),
      6, "No requests yet.");
}}
$("dbg").addEventListener("change", async e => {{
  await fetch(P + "/debug/toggle", {{method: "POST", headers: {{"Content-Type": "application/json", "X-Family-Token": TOKEN}},
    body: JSON.stringify({{enabled: e.target.checked}})}});
  refresh();
}});
refresh(); setInterval(refresh, 3000);
</script></main></body></html>"""
