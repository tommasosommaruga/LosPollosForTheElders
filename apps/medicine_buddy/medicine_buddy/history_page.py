"""History explorer HTML for /medicine/history, people, and record detail pages."""

from __future__ import annotations

import json
from typing import Any


def render_history_page(csrf: str, nav: str, *, base_css: str, prefix: str = "/medicine") -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Medicine Buddy - History</title>
<style>{base_css}
.tabs{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 14px}}
.tabs button{{background:var(--card)}} .tabs button.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.filters{{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin-bottom:12px}}
.filters label{{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}}
.filters input,.filters select{{min-width:120px}}
.banner{{padding:10px 12px;border-radius:8px;margin-bottom:10px;display:none}}
.banner.show{{display:block}} .banner.err{{background:#fdecea;color:var(--warn)}} .banner.info{{background:var(--soft)}}
@media (prefers-color-scheme:dark){{.banner.err{{background:#3a221f}}}}
.pager{{display:flex;gap:10px;align-items:center;justify-content:space-between;margin-top:10px;font-size:13px}}
.empty{{color:var(--muted);padding:12px 0}}
a.rowlink{{color:inherit;text-decoration:none}} a.rowlink:hover{{color:var(--accent)}}
.live{{font-size:12px;color:var(--muted)}}
</style></head><body><main>
{nav}
<h1>History</h1>
<p class=muted>Browse agent activity, search people and stories, and review adherence — all from live data.</p>
<div id=banner class="banner" role="status" aria-live="polite"></div>
<div class=tabs role=tablist>
  <button type=button class=active data-tab=activity aria-selected=true>Activity</button>
  <button type=button data-tab=search aria-selected=false>Search</button>
  <button type=button data-tab=people aria-selected=false>People</button>
  <button type=button data-tab=reports aria-selected=false>Reports</button>
</div>

<section id=panel-activity>
  <h2>Activity timeline</h2>
  <div class=filters>
    <label>Person<select id=a-person aria-label="Filter by person"><option value="">Anyone</option></select></label>
    <label>From<input id=a-via placeholder="via (api / app-ui)" aria-label="Filter by via"></label>
    <label>Status<select id=a-status aria-label="Filter by status"><option value="">Any</option>
      <option value=ok>OK</option><option value=error>Error</option></select></label>
    <label>Path<input id=a-path placeholder="/schedules" aria-label="Path prefix"></label>
    <label>From date<input id=a-from type=date aria-label="From date"></label>
    <label>To date<input id=a-to type=date aria-label="To date"></label>
    <button type=button class=primary id=a-apply>Apply</button>
    <span class=live id=a-live></span>
  </div>
  <table><thead><tr><th>When</th><th>From</th><th>Request</th><th>Status</th><th>ms</th><th>Person</th><th>Outcome</th></tr></thead>
  <tbody id=a-body></tbody></table>
  <div class=pager><span id=a-total class=muted></span>
    <span><button type=button id=a-prev>Prev</button> <button type=button id=a-next>Next</button></span></div>
</section>

<section id=panel-search hidden>
  <h2>Search</h2>
  <div class=filters>
    <label>Query<input id=s-q placeholder="name, story, medicine…" aria-label="Search query"></label>
    <label>Person<select id=s-person aria-label="Search person"><option value="">Anyone</option></select></label>
    <label>Kind<select id=s-kind aria-label="Result kind"><option value="">All</option>
      <option>person</option><option>memory</option><option>story</option><option>dose</option>
      <option>medicine</option><option>activity</option></select></label>
    <label>From<input id=s-from type=date aria-label="Search from"></label>
    <label>To<input id=s-to type=date aria-label="Search to"></label>
    <button type=button class=primary id=s-go>Search</button>
  </div>
  <table><thead><tr><th>Kind</th><th>Title</th><th>Person</th><th>Snippet</th></tr></thead>
  <tbody id=s-body></tbody></table>
  <div class=pager><span id=s-total class=muted></span>
    <span><button type=button id=s-prev>Prev</button> <button type=button id=s-next>Next</button></span></div>
</section>

<section id=panel-people hidden>
  <h2>People</h2>
  <table><thead><tr><th>Name</th><th>Role</th><th>Medicines</th><th>Stories</th><th>Doses</th></tr></thead>
  <tbody id=p-body></tbody></table>
</section>

<section id=panel-reports hidden>
  <h2>Adherence</h2>
  <div class=filters>
    <label>Person<select id=r-person aria-label="Report person"><option value="">Everyone</option></select></label>
    <label>Days<select id=r-days aria-label="Report days"><option value=7>7</option><option value=30>30</option></select></label>
    <button type=button class=primary id=r-go>Show</button>
  </div>
  <div class=kv id=r-kv></div>
  <p class=muted id=r-note></p>
</section>

<section>
  <h2>Agent status</h2>
  <div class=kv id=agent-kv></div>
</section>

<script>
const TOKEN = {json.dumps(csrf)};
const P = {json.dumps(prefix)};
const $ = id => document.getElementById(id);
function showBanner(msg, isErr) {{
  const b = $("banner");
  if (!msg) {{ b.className = "banner"; b.textContent = ""; return; }}
  b.textContent = msg;
  b.className = "banner show " + (isErr ? "err" : "info");
}}
async function api(path) {{
  const r = await fetch(P + path, {{cache: "no-store"}});
  if (r.status === 401) {{ location.href = P; throw new Error("signed out"); }}
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}}
function td(text, cls) {{
  const c = document.createElement("td");
  c.textContent = text == null ? "" : String(text);
  if (cls) c.className = cls;
  return c;
}}
function emptyRow(cols, msg) {{
  const tr = document.createElement("tr");
  const c = td(msg, "muted"); c.colSpan = cols; tr.append(c); return tr;
}}
function relTime(isoOrTs) {{
  let t = typeof isoOrTs === "number" ? isoOrTs * 1000 : Date.parse(isoOrTs);
  if (!t) return "";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s/60) + "m ago";
  if (s < 86400) return Math.floor(s/3600) + "h ago";
  return Math.floor(s/86400) + "d ago";
}}
function outcome(row) {{
  return [row.blocked ? "BLOCKED" : "", row.error || "", row.final_state || "", row.result || "",
          row.utterance_chars != null ? (row.utterance_chars + " chars") : "",
          row.speak_hint || ""].filter(Boolean).join(" | ");
}}

let aOffset = 0, aLimit = 40, aSinceId = null, aMode = "page";
async function loadPeopleSelects() {{
  const d = await api("/api/people");
  for (const id of ["a-person", "s-person", "r-person"]) {{
    const sel = $(id);
    const keep = sel.value;
    sel.replaceChildren(new Option(id === "r-person" ? "Everyone" : "Anyone", ""));
    for (const p of d.items) sel.append(new Option(p.name + (p.is_caregiver ? " (caregiver)" : ""), String(p.id)));
    sel.value = keep;
  }}
}}
function aParams(extra) {{
  const q = new URLSearchParams();
  if ($("a-person").value) q.set("person", $("a-person").value);
  if ($("a-via").value.trim()) q.set("via", $("a-via").value.trim());
  if ($("a-status").value) q.set("status_class", $("a-status").value);
  if ($("a-path").value.trim()) q.set("path", $("a-path").value.trim());
  if ($("a-from").value) q.set("from", $("a-from").value);
  if ($("a-to").value) q.set("to", $("a-to").value + "T23:59:59");
  q.set("limit", String(aLimit));
  if (extra) for (const [k,v] of Object.entries(extra)) if (v != null) q.set(k, String(v));
  return q;
}}
async function loadActivity(opts) {{
  opts = opts || {{}};
  try {{
    showBanner("");
    let q;
    if (opts.cursor && aSinceId != null) {{
      q = aParams({{since_id: aSinceId, offset: 0}});
      aMode = "cursor";
    }} else {{
      q = aParams({{offset: aOffset}});
      aMode = "page";
    }}
    const d = await api("/api/activity?" + q.toString());
    const body = $("a-body");
    if (opts.cursor && d.items.length) {{
      // prepend new rows without wiping the table
      const frag = document.createDocumentFragment();
      for (const row of d.items.slice().reverse()) frag.append(activityRow(row));
      body.prepend(frag);
      aSinceId = d.next_since_id;
    }} else {{
      if (!d.items.length) body.replaceChildren(emptyRow(7, "No activity yet."));
      else body.replaceChildren(...d.items.map(activityRow));
      if (d.items.length) aSinceId = Math.max(...d.items.map(i => i.id), aSinceId || 0);
      $("a-total").textContent = d.total + " matching · showing " + d.items.length;
    }}
    $("a-live").textContent = "Updated " + new Date().toLocaleTimeString();
  }} catch (e) {{ showBanner(e.message || String(e), true); }}
}}
function activityRow(row) {{
  const tr = document.createElement("tr");
  const link = document.createElement("a");
  link.className = "rowlink"; link.href = P + "/records/activity/" + row.id;
  link.textContent = row.method + " " + row.path;
  const req = document.createElement("td"); req.append(link);
  tr.append(
    td(relTime(row.at_ts || row.at), "muted"),
    td(row.via),
    req,
    td(row.status, row.status >= 400 ? "bad" : "ok"),
    td(row.ms),
    td(row.person_name || ""),
    td(outcome(row))
  );
  return tr;
}}

let sOffset = 0, sLimit = 30;
async function loadSearch() {{
  try {{
    showBanner("");
    const q = new URLSearchParams();
    if ($("s-q").value.trim()) q.set("q", $("s-q").value.trim());
    if ($("s-person").value) q.set("person", $("s-person").value);
    if ($("s-kind").value) q.set("kind", $("s-kind").value);
    if ($("s-from").value) q.set("from", $("s-from").value);
    if ($("s-to").value) q.set("to", $("s-to").value + "T23:59:59");
    q.set("limit", String(sLimit)); q.set("offset", String(sOffset));
    const d = await api("/api/search?" + q.toString());
    const body = $("s-body");
    if (!d.items.length) body.replaceChildren(emptyRow(4, "No matches."));
    else body.replaceChildren(...d.items.map(item => {{
      const tr = document.createElement("tr");
      const href = item.kind === "person" ? (P + "/people/" + item.id)
                 : item.kind === "memory" ? (P + "/records/memory/" + item.id)
                 : item.kind === "dose" ? (P + "/records/dose_event/" + item.id)
                 : item.kind === "medicine" ? (P + "/records/medicine/" + item.id)
                 : item.kind === "activity" ? (P + "/records/activity/" + item.id)
                 : "#";
      const a = document.createElement("a"); a.className = "rowlink"; a.href = href; a.textContent = item.title || "";
      const titleTd = document.createElement("td"); titleTd.append(a);
      tr.append(td(item.kind), titleTd, td(item.person_name || ""), td(item.snippet || ""));
      return tr;
    }}));
    $("s-total").textContent = d.total + " results";
  }} catch (e) {{ showBanner(e.message || String(e), true); }}
}}

async function loadPeople() {{
  try {{
    const d = await api("/api/people");
    const body = $("p-body");
    if (!d.items.length) body.replaceChildren(emptyRow(5, "No people registered."));
    else body.replaceChildren(...d.items.map(p => {{
      const tr = document.createElement("tr");
      const a = document.createElement("a"); a.className = "rowlink"; a.href = P + "/people/" + p.id; a.textContent = p.name;
      const nameTd = document.createElement("td"); nameTd.append(a);
      tr.append(nameTd, td(p.role), td(p.medicines), td(p.memories), td(p.dose_events));
      return tr;
    }}));
  }} catch (e) {{ showBanner(e.message || String(e), true); }}
}}

async function loadReport() {{
  try {{
    const q = new URLSearchParams();
    if ($("r-person").value) q.set("person", $("r-person").value);
    q.set("days", $("r-days").value || "7");
    const d = await api("/api/reports/adherence?" + q.toString());
    const kv = $("r-kv");
    kv.replaceChildren();
    for (const [k, v] of Object.entries({{
      person: d.person_name || "Everyone", days: d.days, taken: d.taken, missed: d.missed,
      failed: d.failed, adherence: d.adherence_pct == null ? "n/a" : (d.adherence_pct + "%"),
      since: d.since
    }})) {{
      const div = document.createElement("div");
      const a = document.createElement("span"); a.textContent = k;
      const b = document.createElement("span"); b.textContent = String(v);
      div.append(a, b); kv.append(div);
    }}
    $("r-note").textContent = d.total_terminal
      ? "Based on Taken / Missed / Failed dose events only."
      : "No terminal dose events in this window yet.";
  }} catch (e) {{ showBanner(e.message || String(e), true); }}
}}

async function loadAgent() {{
  try {{
    const d = await api("/api/agent");
    const kv = $("agent-kv");
    kv.replaceChildren();
    const rm = d.reminders || {{}};
    const rows = {{
      now: d.now, uptime_s: d.uptime_s, dry_run: d.app && d.app.dry_run,
      reminders_running: !!rm.running, voice_guard: d.voice_guard && d.voice_guard.enabled,
      people: d.data && d.data.people, memories: d.data && d.data.memories,
      dose_events: d.data && d.data.dose_events, activity_hint: "see Activity tab"
    }};
    for (const [k, v] of Object.entries(rows)) {{
      const div = document.createElement("div");
      const a = document.createElement("span"); a.textContent = k.replaceAll("_", " ");
      const b = document.createElement("span"); b.textContent = v == null ? "-" : String(v);
      div.append(a, b); kv.append(div);
    }}
  }} catch (e) {{ /* non-fatal on history page */ }}
}}

document.querySelectorAll(".tabs button").forEach(btn => {{
  btn.addEventListener("click", () => {{
    document.querySelectorAll(".tabs button").forEach(b => {{ b.classList.toggle("active", b === btn); b.setAttribute("aria-selected", b === btn ? "true" : "false"); }});
    for (const id of ["activity", "search", "people", "reports"]) {{
      $("panel-" + id).hidden = btn.dataset.tab !== id;
    }}
    if (btn.dataset.tab === "people") loadPeople();
    if (btn.dataset.tab === "reports") loadReport();
    if (btn.dataset.tab === "search" && !$("s-body").children.length) loadSearch();
  }});
}});
$("a-apply").onclick = () => {{ aOffset = 0; aSinceId = null; loadActivity(); }};
$("a-prev").onclick = () => {{ aOffset = Math.max(0, aOffset - aLimit); loadActivity(); }};
$("a-next").onclick = () => {{ aOffset = aOffset + aLimit; loadActivity(); }};
$("s-go").onclick = () => {{ sOffset = 0; loadSearch(); }};
$("s-prev").onclick = () => {{ sOffset = Math.max(0, sOffset - sLimit); loadSearch(); }};
$("s-next").onclick = () => {{ sOffset = sOffset + sLimit; loadSearch(); }};
$("r-go").onclick = loadReport;

(async () => {{
  await loadPeopleSelects();
  await loadActivity();
  await loadAgent();
  setInterval(() => loadActivity({{cursor: true}}), 4000);
  setInterval(loadAgent, 15000);
}})();
</script></main></body></html>"""


def render_person_page(
    csrf: str,
    nav: str,
    person_id: int,
    dossier: dict[str, Any] | None,
    *,
    base_css: str,
    prefix: str = "/medicine",
) -> str:
    if dossier is None or "error" in (dossier or {}):
        msg = (dossier or {}).get("error") or "Person not found"
        return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Person</title>
<style>{base_css}</style></head><body><main>
{nav}<h1>Person</h1><section><p class=bad>{_esc(msg)}</p>
<p><a href="{prefix}/history">Back to History</a></p></section></main></body></html>"""
    person = dossier["person"]
    name = _esc(person["name"])
    role = _esc(person["role"])
    counts = dossier.get("counts") or {}
    mem_rows = "".join(
        f'<tr><td>{_esc(m["kind"])}</td><td><a href="{prefix}/records/memory/{m["id"]}">{_esc(m["title"])}</a></td>'
        f'<td class=muted>{_esc(m["created_at"])}</td></tr>'
        for m in dossier.get("memories") or []
    ) or '<tr><td colspan=3 class=muted>No stories yet.</td></tr>'
    med_rows = "".join(
        f'<tr><td><a href="{prefix}/records/medicine/{m["id"]}">{_esc(m["name"])}</a></td></tr>'
        for m in dossier.get("medicines") or []
    ) or '<tr><td class=muted>No medicines.</td></tr>'
    sched_rows = "".join(
        f'<tr><td>{_esc(s["medicine_name"])}</td><td>{_esc(s["weekdays"])}</td>'
        f'<td>{_esc(s["time_hhmm"])}</td><td>{"active" if s["active"] else "off"}</td></tr>'
        for s in dossier.get("schedules") or []
    ) or '<tr><td colspan=4 class=muted>No schedules.</td></tr>'
    dose_rows = "".join(
        f'<tr><td class=muted>{_esc(d["created_at"])}</td>'
        f'<td><a href="{prefix}/records/dose_event/{d["id"]}">{_esc(d["medicine_name"])}</a></td>'
        f'<td>{_esc(d["state"])}</td><td>{_esc(d.get("detail") or "")}</td></tr>'
        for d in dossier.get("dose_events") or []
    ) or '<tr><td colspan=4 class=muted>No dose events.</td></tr>'
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>{name} - Medicine Buddy</title><style>{base_css}</style></head><body><main>
{nav}<h1>{name}</h1>
<p class=muted>Role: {role} · medicines {counts.get("medicines", 0)} · stories {counts.get("memories", 0)} ·
doses {counts.get("dose_events", 0)} · activity {counts.get("activity", 0)}</p>
<section><h2>Stories &amp; memories</h2>
<table><thead><tr><th>Kind</th><th>Title</th><th>When</th></tr></thead><tbody>{mem_rows}</tbody></table></section>
<section><h2>Medicines</h2>
<table><thead><tr><th>Name</th></tr></thead><tbody>{med_rows}</tbody></table></section>
<section><h2>Schedules</h2>
<table><thead><tr><th>Medicine</th><th>Days</th><th>Time</th><th>Status</th></tr></thead><tbody>{sched_rows}</tbody></table></section>
<section><h2>Recent doses</h2>
<table><thead><tr><th>When</th><th>Medicine</th><th>State</th><th>Detail</th></tr></thead><tbody>{dose_rows}</tbody></table></section>
<p><a href="{prefix}/history">Back to History</a></p>
</main></body></html>"""


def render_record_page(
    csrf: str,
    nav: str,
    kind: str,
    record: dict[str, Any],
    *,
    base_css: str,
    prefix: str = "/medicine",
) -> str:
    if "error" in record:
        return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Record</title>
<style>{base_css}</style></head><body><main>
{nav}<h1>Record</h1><section><p class=bad>{_esc(record["error"])}</p>
<p><a href="{prefix}/history">Back to History</a></p></section></main></body></html>"""

    title = _esc(str(record.get("title") or record.get("name") or f"{kind} #{record.get('id')}"))
    body_bits: list[str] = []
    if kind == "memory" or record.get("kind") == "memory":
        body_bits.append(
            f"<p class=muted>{_esc(record.get('memory_kind'))} · {_esc(record.get('person_name'))} · "
            f"{_esc(record.get('created_at'))}</p>"
        )
        body_bits.append(
            f"<section><h2>{title}</h2><p style='white-space:pre-wrap'>{_esc(record.get('text') or '')}</p></section>"
        )
        if record.get("person_id"):
            body_bits.append(
                f'<p><a href="{prefix}/people/{record["person_id"]}">'
                f'View {_esc(record.get("person_name") or "person")}</a></p>'
            )
    elif record.get("kind") == "dose_event" or kind in {"dose", "dose_event"}:
        body_bits.append(
            f"<p class=muted>{_esc(record.get('medicine_name'))} · {_esc(record.get('person_name'))} · "
            f"{_esc(record.get('state'))} · {_esc(record.get('created_at'))}</p>"
        )
        body_bits.append(f"<section><h2>Detail</h2><p>{_esc(record.get('detail') or '—')}</p></section>")
        tl = "".join(
            f"<tr><td class=muted>{_esc(t['created_at'])}</td><td>{_esc(t['state'])}</td>"
            f"<td>{_esc(t.get('detail') or '')}</td></tr>"
            for t in record.get("timeline") or []
        ) or '<tr><td colspan=3 class=muted>No timeline.</td></tr>'
        body_bits.append(
            f"<section><h2>State timeline (this medicine)</h2>"
            f"<table><thead><tr><th>When</th><th>State</th><th>Detail</th></tr></thead>"
            f"<tbody>{tl}</tbody></table></section>"
        )
    elif record.get("kind") == "activity" or kind == "activity":
        chars = record.get("utterance_chars")
        body_bits.append(
            f"<section><h2>{_esc(record.get('method'))} {_esc(record.get('path'))}</h2>"
            f"<div class=kv>"
            f"<div><span>when</span><span>{_esc(record.get('at'))}</span></div>"
            f"<div><span>via</span><span>{_esc(record.get('via'))}</span></div>"
            f"<div><span>status</span><span>{_esc(record.get('status'))}</span></div>"
            f"<div><span>ms</span><span>{_esc(record.get('ms'))}</span></div>"
            f"<div><span>person</span><span>{_esc(record.get('person_name') or '—')}</span></div>"
            f"<div><span>result</span><span>{_esc(record.get('result') or '—')}</span></div>"
            f"<div><span>error</span><span>{_esc(record.get('error') or '—')}</span></div>"
            f"<div><span>final state</span><span>{_esc(record.get('final_state') or '—')}</span></div>"
            f"<div><span>utterance chars</span><span>{_esc('—' if chars is None else chars)}</span></div>"
            f"</div></section>"
        )
    elif record.get("kind") == "medicine" or kind == "medicine":
        body_bits.append(f"<p class=muted>For {_esc(record.get('person_name') or 'unassigned')}</p>")
        sched = "".join(
            f"<tr><td>{_esc(s['weekdays'])}</td><td>{_esc(s['time_hhmm'])}</td>"
            f"<td>{'active' if s['active'] else 'off'}</td></tr>"
            for s in record.get("schedules") or []
        ) or '<tr><td colspan=3 class=muted>No schedules.</td></tr>'
        body_bits.append(
            f"<section><h2>{title}</h2>"
            f"<table><thead><tr><th>Days</th><th>Time</th><th>Status</th></tr></thead>"
            f"<tbody>{sched}</tbody></table></section>"
        )
    else:
        body_bits.append(
            f"<section><pre style='white-space:pre-wrap'>"
            f"{_esc(json.dumps(record, indent=2, default=str))}</pre></section>"
        )

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>{title} - Medicine Buddy</title><style>{base_css}</style></head><body><main>
{nav}<h1>Record</h1>
{''.join(body_bits)}
<p><a href="{prefix}/history">Back to History</a></p>
</main></body></html>"""


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
