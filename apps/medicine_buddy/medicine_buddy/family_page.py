"""The Family settings page (GET /family): add people, change passphrases by typing.

Plain HTML + inline JS, no external resources. All text from the server is inserted
with textContent (never innerHTML), and every request carries the per-start token.
"""

from __future__ import annotations

import json


def render_family_page(token: str, base: str = "", nav: str | None = None) -> str:
    """base: URL prefix of the settings routes ('' on :8787, '/medicine' in the app UI).
    nav: optional navigation bar HTML replacing the default back link."""
    if nav is None:
        nav = '<p><a href="/">&larr; Medicine Buddy</a></p>'
    return (_PAGE.replace("__TOKEN__", json.dumps(token))
                 .replace("__BASE__", json.dumps(base))
                 .replace("<!--NAV-->", nav))


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Family settings</title>
<style>
:root{--bg:#f6f4ef;--card:#fff;--fg:#1f1d19;--muted:#6f6b62;--line:#e3dfd5;--accent:#2f6f4f;--accent-fg:#fff;
--soft:#eaf3ee;--warn:#b3412f;--chip:#fff7e0;--chip-line:#ecd9a0}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--card:#1f1e1b;--fg:#efede7;--muted:#a19d93;--line:#34322d;
--accent:#7cc39c;--accent-fg:#10251a;--soft:#1d2a22;--warn:#ef8a78;--chip:#2c2717;--chip-line:#5b4d22}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 16px 60px}
a{color:var(--accent)}
h1{font-size:28px;margin:0 0 6px} h2{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 22px}
.notice{display:flex;gap:12px;align-items:flex-start;background:var(--soft);border-radius:12px;padding:14px 16px;margin-bottom:22px}
.notice b{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:26px}
.person{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:8px}
.person .name{font-size:20px;font-weight:650}
.badge{display:inline-block;font-size:13px;font-weight:600;border-radius:999px;padding:2px 10px;background:var(--soft);color:var(--accent)}
.badge.care{background:var(--accent);color:var(--accent-fg)}
.meta{color:var(--muted);font-size:14px}
button{font:inherit;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);padding:10px 14px;cursor:pointer}
button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent);font-weight:600}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
section.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:18px}
.steps{counter-reset:s;display:grid;gap:16px;margin-top:14px}
.step{display:grid;gap:6px}
.step>label{font-weight:600}
.step>label::before{counter-increment:s;content:counter(s);display:inline-grid;place-items:center;width:26px;height:26px;
border-radius:50%;background:var(--soft);color:var(--accent);font-size:14px;margin-right:8px}
.hint{color:var(--muted);font-size:14px}
.row{display:flex;gap:8px;flex-wrap:wrap}
input,select{font:inherit;width:100%;padding:11px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.pw{position:relative} .pw input{padding-right:78px}
.pw button{position:absolute;right:6px;top:50%;transform:translateY(-50%);padding:5px 10px;font-size:14px}
.chips{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0}
.chip{background:var(--chip);border:1px solid var(--chip-line);border-radius:12px;padding:10px 16px;font-size:24px;font-weight:650;letter-spacing:.01em}
.meter{height:8px;border-radius:99px;background:var(--line);overflow:hidden}
.meter>div{height:100%;width:0;background:var(--warn);transition:width .2s}
.msg{border-radius:10px;padding:12px 14px;margin-top:14px;display:none}
.msg.ok{display:block;background:var(--soft);color:var(--accent);font-weight:600}
.msg.err{display:block;background:transparent;border:1px solid var(--warn);color:var(--warn)}
[hidden]{display:none!important}
</style></head><body><main>
<!--NAV-->
<h1>Family &amp; passphrases</h1>
<p class="sub">Each person's passphrase protects their own medicine timetable and memories.</p>

<div class="notice"><div>&#128274;</div><div>
<b>Typed here, never spoken.</b>
Passphrases you type on this page never pass through Reachy's AI and are stored only as a scrambled
fingerprint. This page works only on this computer.
</div></div>

<h2>People</h2>
<div id="people" class="grid"><div class="meta">Loading&hellip;</div></div>

<section class="panel" id="change" hidden>
  <h2>Change <span id="ch-name"></span>'s passphrase</h2>
  <div class="steps">
    <div class="step"><label for="ch-current">Prove it's you</label>
      <div class="pw"><input id="ch-current" type="password" autocomplete="off"><button type="button" data-show="ch-current">Show</button></div>
      <span class="hint">Type the current passphrase. Forgot it? The caregiver's passphrase works too.</span></div>
    <div class="step"><label for="ch-new">Choose the new passphrase</label>
      <div class="row"><button type="button" id="ch-suggest">&#127922; Suggest an easy one</button></div>
      <div class="chips" id="ch-chips" hidden></div>
      <span class="hint" id="ch-chips-hint" hidden>Say it out loud three times to remember it. Easy words like these are understood well by Reachy.</span>
      <div class="pw"><input id="ch-new" type="password" autocomplete="new-password"><button type="button" data-show="ch-new">Show</button></div>
      <div class="meter"><div id="ch-meter"></div></div><span class="hint" id="ch-strength">At least 3 words is best.</span></div>
    <div class="step"><label for="ch-repeat">Type it again</label>
      <div class="pw"><input id="ch-repeat" type="password" autocomplete="new-password"><button type="button" data-show="ch-repeat">Show</button></div></div>
    <div class="row"><button class="primary" id="ch-save">Save new passphrase</button><button type="button" id="ch-cancel">Cancel</button></div>
  </div>
  <div class="msg" id="ch-msg"></div>
</section>

<section class="panel">
  <h2>Add someone</h2>
  <p class="hint">People can also do this by voice: the first time Reachy saves something for them, it asks them to choose a passphrase and say it twice.</p>
  <div class="steps">
    <div class="step"><label for="ad-name">Name</label><input id="ad-name" autocomplete="off" placeholder="e.g. Mario"></div>
    <div class="step"><label for="ad-role">Role</label>
      <select id="ad-role"><option value="person">Family member (their own medicines &amp; memories)</option>
      <option value="caregiver">Caregiver (can manage everyone)</option></select></div>
    <div class="step" id="ad-care-step" hidden><label for="ad-care">An existing caregiver's passphrase</label>
      <div class="pw"><input id="ad-care" type="password" autocomplete="off"><button type="button" data-show="ad-care">Show</button></div>
      <span class="hint">Only a caregiver can make someone else a caregiver.</span></div>
    <div class="step"><label for="ad-new">Their passphrase</label>
      <div class="row"><button type="button" id="ad-suggest">&#127922; Suggest an easy one</button></div>
      <div class="chips" id="ad-chips" hidden></div>
      <div class="pw"><input id="ad-new" type="password" autocomplete="new-password"><button type="button" data-show="ad-new">Show</button></div>
      <div class="meter"><div id="ad-meter"></div></div><span class="hint" id="ad-strength">At least 3 words is best.</span></div>
    <div class="step"><label for="ad-repeat">Type it again</label>
      <div class="pw"><input id="ad-repeat" type="password" autocomplete="new-password"><button type="button" data-show="ad-repeat">Show</button></div></div>
    <div class="row"><button class="primary" id="ad-save">Add person</button></div>
  </div>
  <div class="msg" id="ad-msg"></div>
</section>
</main>
<script>
const TOKEN = __TOKEN__;
const BASE = __BASE__;
const $ = id => document.getElementById(id);

async function api(path, body) {
  const r = await fetch(BASE + path, {method: "POST", headers: {"Content-Type": "application/json", "X-Family-Token": TOKEN},
                               body: JSON.stringify(body || {})});
  let data = {};
  try { data = await r.json(); } catch (e) { data = {error: "Unexpected answer from the server"}; }
  if (!r.ok && !data.error) data.error = "Request failed (" + r.status + ")";
  return data;
}

function msg(el, text, ok) { el.textContent = text; el.className = "msg " + (ok ? "ok" : "err"); }
function clearMsg(el) { el.textContent = ""; el.className = "msg"; }

function strength(input, bar, label) {
  const words = input.value.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").trim().split(/\s+/).filter(Boolean);
  const letters = input.value.replace(/[^a-zA-Z]/g, "").length;
  const unique = new Set(words).size;
  let score = Math.min(unique, 4) * 22 + Math.min(letters, 20);
  if (words.length < 2 || letters < 10) score = Math.min(score, 25);
  bar.style.width = Math.min(score, 100) + "%";
  bar.style.background = score >= 80 ? "var(--accent)" : score >= 50 ? "#d4a017" : "var(--warn)";
  label.textContent = words.length < 2 ? "Use at least 2 words (3 or 4 is best)."
    : unique < 3 ? "Good - a third word makes it much stronger." : "Strong and easy to say.";
}

function showChips(container, phrase) {
  container.replaceChildren(...phrase.split(" ").map(w => { const s = document.createElement("span");
    s.className = "chip"; s.textContent = w; return s; }));
  container.hidden = false;
}

async function suggestInto(a, b, chips, hint, bar, label) {
  const r = await api("/settings/suggest");
  if (!r.passphrase) return;
  a.value = r.passphrase; b.value = r.passphrase;
  showChips(chips, r.passphrase); if (hint) hint.hidden = false;
  strength(a, bar, label);
}

document.querySelectorAll("[data-show]").forEach(btn => btn.addEventListener("click", () => {
  const input = $(btn.dataset.show); const show = input.type === "password";
  input.type = show ? "text" : "password"; btn.textContent = show ? "Hide" : "Show";
}));

let changing = null;
async function loadPeople() {
  const r = await api("/settings/people");
  const box = $("people");
  if (!r.people || !r.people.length) {
    const p = document.createElement("div"); p.className = "meta";
    p.textContent = "Nobody yet. Add the caregiver below, or let people set up their passphrase by voice.";
    box.replaceChildren(p); return;
  }
  box.replaceChildren(...r.people.map(p => {
    const card = document.createElement("div"); card.className = "person";
    const name = document.createElement("div"); name.className = "name"; name.textContent = p.name;
    const badge = document.createElement("span"); badge.className = "badge" + (p.role === "caregiver" ? " care" : "");
    badge.textContent = p.role === "caregiver" ? "Caregiver" : "Family member";
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = p.medicines + " medicine" + (p.medicines === 1 ? "" : "s") + " · " +
                       p.memories + " memor" + (p.memories === 1 ? "y" : "ies");
    const btn = document.createElement("button"); btn.textContent = "Change passphrase";
    btn.addEventListener("click", () => openChange(p.name));
    card.append(name, badge, meta, btn); return card;
  }));
}

function openChange(name) {
  changing = name; $("ch-name").textContent = name;
  ["ch-current", "ch-new", "ch-repeat"].forEach(id => { $(id).value = ""; $(id).type = "password"; });
  $("ch-chips").hidden = true; $("ch-chips-hint").hidden = true; clearMsg($("ch-msg"));
  strength($("ch-new"), $("ch-meter"), $("ch-strength"));
  $("change").hidden = false; $("change").scrollIntoView({behavior: "smooth"}); $("ch-current").focus();
}

$("ch-cancel").addEventListener("click", () => { $("change").hidden = true; changing = null; });
$("ch-new").addEventListener("input", () => strength($("ch-new"), $("ch-meter"), $("ch-strength")));
$("ad-new").addEventListener("input", () => strength($("ad-new"), $("ad-meter"), $("ad-strength")));
$("ch-suggest").addEventListener("click", () =>
  suggestInto($("ch-new"), $("ch-repeat"), $("ch-chips"), $("ch-chips-hint"), $("ch-meter"), $("ch-strength")));
$("ad-suggest").addEventListener("click", () =>
  suggestInto($("ad-new"), $("ad-repeat"), $("ad-chips"), null, $("ad-meter"), $("ad-strength")));
$("ad-role").addEventListener("change", () => { $("ad-care-step").hidden = $("ad-role").value !== "caregiver"; });

$("ch-save").addEventListener("click", async () => {
  const r = await api("/settings/passphrase", {name: changing, current: $("ch-current").value,
                                               new: $("ch-new").value, new_repeat: $("ch-repeat").value});
  if (r.status === "ok") {
    msg($("ch-msg"), r.message + " Reachy will only accept the new one from now on.", true);
    ["ch-current", "ch-new", "ch-repeat"].forEach(id => $(id).value = "");
  } else msg($("ch-msg"), r.error, false);
});

$("ad-save").addEventListener("click", async () => {
  const r = await api("/settings/register", {name: $("ad-name").value, role: $("ad-role").value,
      passphrase: $("ad-new").value, passphrase_repeat: $("ad-repeat").value, caregiver_passphrase: $("ad-care").value});
  if (r.status === "ok") {
    msg($("ad-msg"), r.person.name + " was added. They can now use their passphrase with Reachy.", true);
    ["ad-name", "ad-new", "ad-repeat", "ad-care"].forEach(id => $(id).value = ""); $("ad-chips").hidden = true;
    loadPeople();
  } else msg($("ad-msg"), r.error, false);
});

loadPeople();
</script></body></html>"""
