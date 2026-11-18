"use strict";
// Dependency-free SPA: SSE chat over fetch, EN/AR with RTL, citations, answer checks, graph context,
// follow-ups, a knowledge-graph explorer and a lead form.

const $ = (s, el = document) => el.querySelector(s);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const state = {
  lang: store.get("lang") || ((navigator.language || "en").startsWith("ar") ? "ar" : "en"),
  L: {}, cfg: null, history: [], busy: false,
  session: store.get("session") || (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)),
};
store.set("session", state.session);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(t, vars) { return String(t || "").replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m)); }
function num(n) { return n == null ? "—" : Number(n).toLocaleString(state.lang === "ar" ? "ar-SA" : "en-GB"); }
function date(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleDateString(state.lang === "ar" ? "ar-SA" : "en-GB", { day: "numeric", month: "short", year: "numeric" }); }
  catch { return iso.slice(0, 10); }
}

function brandClass(key, base = "tag") {
  const brands = (state.cfg?.brands || []).filter(b => b.role !== "parent");
  const b = (state.cfg?.brands || []).find(x => x.key === key);
  if (b && b.role === "parent") return `${base} parent`;
  return brands.findIndex(x => x.key === key) === 1 ? `${base} b1` : base;
}

// Minimal markdown: paragraphs, bullets, bold, [n] citation markers. Everything is escaped first.
function render(md, cites) {
  const lines = esc(md).split(/\n/);
  let html = "", list = false;
  for (const raw of lines) {
    const line = raw.trim();
    const bullet = /^[-*•]\s+/.test(line);
    if (bullet && !list) { html += "<ul>"; list = true; }
    if (!bullet && list) { html += "</ul>"; list = false; }
    if (!line) continue;
    const inner = (bullet ? line.replace(/^[-*•]\s+/, "") : line)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\[(\d+)\]/g, (m, n) => {
        const c = cites && cites.find(x => x.n === Number(n));
        return c ? `<a class="cite" href="${esc(c.url)}" target="_blank" rel="noopener" title="${esc(c.title)}">${n}</a>` : `<sup class="cite">${n}</sup>`;
      });
    html += bullet ? `<li>${inner}</li>` : `<p>${inner}</p>`;
  }
  if (list) html += "</ul>";
  return html;
}

async function loadLocale(lang) {
  const r = await fetch(`/api/locale/${lang}`);
  state.L = await r.json();
  state.lang = lang;
  store.set("lang", lang);
  document.documentElement.lang = lang;
  document.documentElement.dir = state.L.dir;
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const v = state.L[el.dataset.i18n];
    if (typeof v === "string") el.textContent = v;
  });
  $("#composer-input").placeholder = state.L.placeholder;
  $("#send").setAttribute("aria-label", state.L.send);
  document.title = state.L.app_title;
  renderHow();
}

function applyTheme(theme) {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const t = theme[dark ? "dark" : "light"];
  const root = document.documentElement.style;
  const map = { bg: "--bg", surface: "--surface", ink: "--ink", muted: "--muted", line: "--line", accent: "--accent",
    accent_ink: "--accent-ink", brand_a: "--brand-a", brand_b: "--brand-b", brand_parent: "--brand-parent" };
  for (const [k, v] of Object.entries(map)) if (t[k]) root.setProperty(v, t[k]);
}

function brandName(b) { return state.lang === "ar" && b.name_ar ? b.name_ar : b.name; }
function brandText(b, key) { return (state.lang === "ar" && b[`${key}_ar`]) || b[key] || ""; }

function renderStats() {
  const s = state.cfg?.stats || {};
  const L = state.L;
  const rows = [
    [num(s.records), L.stat_records], [num(s.chunks), L.stat_chunks], [num(s.entities), L.stat_entities],
    [num(s.relations), L.stat_relations], [date(s.last_fetch), L.stat_updated, true],
  ];
  $("#stats").innerHTML = rows.map(([v, k, small]) => `<div><dd class="${small ? "small" : ""}">${esc(v)}</dd><dt>${esc(k)}</dt></div>`).join("");
}

function renderBrands() {
  const row = $("#brand-row");
  row.innerHTML = state.cfg.brands.map(b => `
    <article class="${brandClass(b.key, "brand-card")}">
      <h3><a href="${esc(b.url)}" target="_blank" rel="noopener">${esc(brandName(b))}</a></h3>
      <p>${esc(brandText(b, "tagline"))}</p>
      <div class="meta"><span>${esc(brandText(b, "coverage"))}</span><span class="count">${esc(fmt(state.L.brand_records, { n: num(b.records) }))}</span></div>
    </article>`).join("");
  $("#lead-brand").innerHTML = state.cfg.brands.filter(b => b.role !== "parent")
    .map(b => `<option value="${esc(b.key)}">${esc(brandName(b))}</option>`).join("");
}

function renderHow() {
  const steps = state.L.how_steps || [];
  $("#how").innerHTML = steps.map(s => `<li><strong>${esc(s.t)}</strong><span>${esc(s.d)}</span></li>`).join("");
}

function renderHosting() {
  const h = state.cfg?.hosting || {};
  if (!h.provider) return;
  const text = fmt(state.L.hosting_note, { provider: h.provider, plan: h.plan, minutes: h.sleeps_after_minutes, seconds: h.cold_start_seconds });
  $("#hosting-note").textContent = text;
  $("#about-hosting").textContent = text;
}

async function loadSuggestions() {
  const box = $("#suggestions");
  box.innerHTML = "";
  try {
    const r = await fetch(`/api/suggestions?lang=${state.lang}`);
    const { items } = await r.json();
    for (const s of items) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "sugg"; b.setAttribute("role", "listitem");
      b.innerHTML = `<span><span class="q">${esc(s.text)}</span><small>${esc(s.brand_name)}</small></span><span class="arrow" aria-hidden="true">→</span>`;
      b.onclick = () => send(s.text);
      box.append(b);
    }
  } catch { /* suggestions are optional */ }
}

async function waitReady() {
  const wake = $("#waking"), started = Date.now();
  let shown = false;
  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch("/readyz", { cache: "no-store" });
      if (r.ok) {
        const j = await r.json();
        wake.hidden = true;
        $("#kb-version").textContent = j.kb_version ? `· KB ${j.kb_version}` : "";
        return true;
      }
    } catch { /* still waking */ }
    if (!shown && Date.now() - started > 600) { wake.hidden = false; shown = true; }
    $("#waking-elapsed").textContent = fmt(state.L.waking_elapsed, { s: Math.round((Date.now() - started) / 1000) });
    await new Promise(r => setTimeout(r, 1500));
  }
  return false;
}

function addUser(text) {
  const li = document.createElement("li");
  li.className = "msg user"; li.textContent = text;
  $("#thread").append(li);
  li.scrollIntoView({ block: "end", behavior: "smooth" });
}

function addBot() {
  const li = document.createElement("li");
  li.className = "msg bot";
  li.innerHTML = `<div class="tags"></div><div class="body typing"></div><div class="extra"></div>`;
  $("#thread").append(li);
  return li;
}

function sourcesBlock(cites) {
  if (!cites.length) return "";
  const items = cites.map(c => `<li value="${c.n}"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>
    <span class="meta">${esc(c.brand_name)} · ${esc(c.source || "")} · ${esc(state.L.as_of)} ${esc(date(c.fetched_at))}</span></li>`).join("");
  return `<details class="panel sources"><summary><span class="panel-label">${esc(state.L.sources)} (${cites.length})</span></summary><ol>${items}</ol></details>`;
}

function checksBlock(g) {
  const L = state.L, names = L.check_names || {};
  const icon = { pass: "✓", warn: "!", fail: "×" };
  const items = g.checks.map(c => {
    let label = names[c.id] || c.id;
    if (c.id === "grounding" && g.grounding?.total) label += ` <b>${g.grounding.supported}/${g.grounding.total}</b>`;
    return `<span class="check ${esc(c.status)}" title="${esc(c.detail || "")}"><i>${icon[c.status] || "·"}</i>${label}</span>`;
  }).join("");
  const verdict = g.status === "pass" ? `<span class="verdict">✓ ${esc(L.verified_label)}</span>` : `<span class="verdict warn">! ${esc(L.review_label)}</span>`;
  return `<div class="panel"><p class="panel-label">${esc(L.checks_title)} · ${verdict}</p><div class="checks">${items}</div></div>`;
}

function graphBlock(g) {
  if (!g.facts?.length) return "";
  const facts = g.facts.slice(0, 6).map(f => `<div class="fact"><span class="node t-${esc(f.a_type)}">${esc(f.a)}</span>
    <span class="rel">${esc(f.label)}</span><span class="node t-${esc(f.b_type)}">${esc(f.b)}</span>
    ${f.cites.map(n => `<sup class="cite">${n}</sup>`).join("")}</div>`).join("");
  return `<div class="panel"><p class="panel-label">${esc(state.L.connections_title)}</p><div class="facts">${facts}</div></div>`;
}

function traceBlock(steps) {
  const names = state.L.trace_steps || {};
  const rows = steps.map(s => {
    const { step, ...rest } = s;
    const v = Object.entries(rest).map(([k, val]) => `${k}=${Array.isArray(val) ? val.join(", ") : val}`).join(" · ");
    return `<li><span class="k">${esc(names[step] || step)}</span><span class="v">${esc(v)}</span></li>`;
  }).join("");
  return `<details class="panel"><summary><span class="panel-label">${esc(state.L.trace_title)}</span></summary><ol class="trace">${rows}</ol></details>`;
}

function followupsBlock(items) {
  return `<div class="panel"><p class="panel-label">${esc(state.L.followups_title)}</p><div class="followups">${items.map(q => `<button type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div></div>`;
}

function money(v, cur) { return v == null ? "" : `${Number(v).toLocaleString(state.lang === "ar" ? "ar-SA" : "en-US")} ${esc(cur || "")}`; }

function cardsBlock(items) {
  const L = state.L;
  return `<div class="cards">${items.map(x => `
    <article class="card">
      <strong>${esc(x.title)}</strong>
      <span class="price">${money(x.price, x.currency)}${x.rent_frequency ? " / " + esc(x.rent_frequency) : ""}</span>
      <span class="muted">${esc([x.district, x.city].filter(Boolean).join(", "))}</span>
      <span class="muted">${[x.bedrooms != null ? `${x.bedrooms} ${L.beds}` : "", x.bathrooms != null ? `${x.bathrooms} ${L.baths}` : "", x.area_sqm ? `${Math.round(x.area_sqm)} ${L.sqm}` : ""].filter(Boolean).join(" · ")}</span>
      ${x.rega_ad_license ? `<span class="muted">${esc(L.rega_verified)}: ${esc(x.rega_ad_license)}</span>` : ""}
      <span class="muted">${esc(L.updated)}: ${esc(date(x.updated_at || x.fetched_at))}</span>
      <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(L.view_listing)} ↗</a>
    </article>`).join("")}</div>`;
}

function handoffBlock(data) {
  const links = (data.contacts || []).map(c => `<a class="ghost" href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name)} ↗</a>`).join("");
  return `<div class="handoff">${links}<button type="button" class="btn lead-open" data-brand="${esc((data.contacts?.[0] || {}).brand || "")}">${esc(state.L.lead_title)}</button></div>`;
}

async function send(text) {
  text = (text || "").trim();
  if (!text || state.busy) return;
  state.busy = true; $("#send").disabled = true;
  $("#intro").hidden = true;
  addUser(text);
  const li = addBot();
  const body = $(".body", li), extra = $(".extra", li), tags = $(".tags", li);
  let answer = "", cites = [], model = null, guard = null, graph = null, trace = null, followups = [];
  const brands = new Set(), notices = [];

  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: state.session, history: state.history.slice(-8), lang: state.lang }),
    });
    if (r.status === 429) throw new Error(state.L.rate_limited);
    if (r.status === 503) { await waitReady(); throw new Error(state.L.ready_error); }
    if (!r.ok || !r.body) throw new Error(state.L.error_generic);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const ev = (raw.match(/^event: (.*)$/m) || [])[1];
        const dataLine = (raw.match(/^data: (.*)$/m) || [])[1];
        if (!ev || dataLine == null) continue;
        const data = JSON.parse(dataLine);
        if (ev === "meta") { li.dir = data.dir; }
        else if (ev === "token") { answer += data.text; body.innerHTML = render(answer, cites); }
        else if (ev === "replace") { answer = data.text; body.innerHTML = render(answer, cites); }
        else if (ev === "citations") { cites = data; data.forEach(c => brands.add(JSON.stringify([c.brand, c.brand_name]))); body.innerHTML = render(answer, cites); }
        else if (ev === "listings") { brands.add(JSON.stringify([data.brand, data.brand_name])); extra.insertAdjacentHTML("beforeend", cardsBlock(data.items)); }
        else if (ev === "handoff") { extra.insertAdjacentHTML("beforeend", handoffBlock(data)); }
        else if (ev === "notice") { notices.push(data.text); }
        else if (ev === "guardrails") { guard = data; }
        else if (ev === "graph") { graph = data; }
        else if (ev === "followups") { followups = data; }
        else if (ev === "trace") { trace = data; }
        else if (ev === "model") { model = data.model; }
        else if (ev === "error") { answer += (answer ? "\n\n" : "") + data.text; body.innerHTML = render(answer, cites); }
      }
    }
  } catch (e) {
    answer = answer || e.message || state.L.error_generic;
    body.innerHTML = render(answer, cites);
  }
  body.classList.remove("typing");
  tags.innerHTML = [...brands].map(s => JSON.parse(s)).map(([k, n]) => `<span class="${brandClass(k)}">${esc(state.L.brand_from.replace("{brand}", n))}</span>`).join("");
  const parts = notices.map(n => `<p class="notice">${esc(n)}</p>`);
  if (graph) parts.push(graphBlock(graph));
  if (guard) parts.push(checksBlock(guard));
  if (cites.some(c => c.source !== "listing")) parts.push(sourcesBlock(cites));
  if (trace && trace.length > 1) parts.push(traceBlock(trace));
  if (followups.length) parts.push(followupsBlock(followups));
  parts.push(`<div class="fb"><button type="button" data-r="1">${esc(state.L.feedback_up)}</button><button type="button" data-r="-1">${esc(state.L.feedback_down)}</button>${model ? `<span class="model">${esc(state.L.model_label)} ${esc(model)}</span>` : ""}</div>`);
  extra.insertAdjacentHTML("beforeend", parts.join(""));
  $(".fb", extra).addEventListener("click", async ev => {
    const btn = ev.target.closest("button[data-r]");
    if (!btn) return;
    await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating: Number(btn.dataset.r), question: text, model: model || "" }) }).catch(() => {});
    $(".fb", extra).innerHTML = `<span>${esc(state.L.feedback_thanks)}</span>`;
  });
  state.history.push({ role: "user", content: text }, { role: "assistant", content: answer.slice(0, 1500) });
  state.busy = false; $("#send").disabled = false;
  li.scrollIntoView({ block: "start", behavior: "smooth" });
}

// ---- knowledge-graph explorer (small force layout, no dependencies) -----------------------------
async function openGraph() {
  const d = $("#graph-dialog");
  d.showModal();
  const stage = $("#graph-stage");
  stage.innerHTML = "";
  let data;
  try { data = await (await fetch(`/api/graph?lang=${state.lang}`)).json(); } catch { data = { nodes: [] }; }
  if (!data.nodes?.length) { stage.innerHTML = `<p class="muted" style="padding:20px">${esc(state.L.graph_empty)}</p>`; return; }
  $("#graph-legend").innerHTML = Object.entries(data.types || {}).filter(([t]) => data.nodes.some(n => n.type === t))
    .map(([t, label]) => `<span><i class="c-${esc(t)}"></i>${esc(label)}</span>`).join("");
  const W = 960, H = 560;  // fixed virtual canvas; the SVG viewBox scales it to the stage
  const nodes = data.nodes.map((n, i) => {
    const a = (i / data.nodes.length) * Math.PI * 2;
    return { ...n, x: W / 2 + Math.cos(a) * W * .3, y: H / 2 + Math.sin(a) * H * .3, vx: 0, vy: 0, r: 4 + Math.min(10, Math.sqrt(n.mentions)) };
  });
  const linked = new Set(data.edges.flatMap(e => [e.a, e.b]));
  for (let i = nodes.length - 1; i >= 0; i--) if (!linked.has(nodes[i].id)) nodes.splice(i, 1);  // isolated nodes add noise
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const edges = data.edges.filter(e => byId[e.a] && byId[e.b]).map(e => ({ ...e, s: byId[e.a], t: byId[e.b] }));
  // Fruchterman-Reingold with linear cooling and a weak pull to the centre.
  const K = Math.sqrt((W * H) / nodes.length) * 0.78, ITER = 420;
  for (let it = 0; it < ITER; it++) {
    const temp = (W / 12) * (1 - it / ITER) + 0.5;
    for (const n of nodes) { n.vx = 0; n.vy = 0; }
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      const dx = a.x - b.x, dy = a.y - b.y, d = Math.sqrt(dx * dx + dy * dy) || 0.1;
      const f = (K * K) / d;
      a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    for (const e of edges) {
      const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.sqrt(dx * dx + dy * dy) || 0.1;
      const f = (d * d) / K * Math.min(1.6, 0.6 + Math.log1p(e.weight) / 3);
      e.s.vx += dx / d * f; e.s.vy += dy / d * f; e.t.vx -= dx / d * f; e.t.vy -= dy / d * f;
    }
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * 0.05; n.vy += (H / 2 - n.y) * 0.1;
      const m = Math.sqrt(n.vx * n.vx + n.vy * n.vy) || 1;
      n.x += n.vx / m * Math.min(m, temp); n.y += n.vy / m * Math.min(m, temp);
      n.x = Math.max(30, Math.min(W - 110, n.x)); n.y = Math.max(20, Math.min(H - 20, n.y));
    }
  }
  const labelMin = [...nodes].sort((a, b) => b.mentions - a.mentions)[Math.min(nodes.length - 1, 28)].mentions;
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(state.L.graph_title)}">
    ${edges.map((e, i) => `<line data-i="${i}" x1="${e.s.x.toFixed(1)}" y1="${e.s.y.toFixed(1)}" x2="${e.t.x.toFixed(1)}" y2="${e.t.y.toFixed(1)}"><title>${esc(e.s.name)} — ${esc(e.label)} — ${esc(e.t.name)}</title></line>`).join("")}
    ${nodes.map(n => `<g class="n" data-id="${esc(n.id)}" tabindex="0" transform="translate(${n.x.toFixed(1)},${n.y.toFixed(1)})">
      <circle r="${n.r.toFixed(1)}" class="c-${esc(n.type)}"></circle>
      <text x="${(n.r + 4).toFixed(1)}" y="3.5" ${n.mentions >= labelMin ? "" : 'visibility="hidden"'}>${esc(n.name)}</text>
      <title>${esc(n.name)} · ${esc((data.types || {})[n.type] || n.type)}</title></g>`).join("")}
  </svg>`;
  stage.innerHTML = svg;
  const svgEl = $("svg", stage);
  const highlight = id => {
    const keep = new Set([id]);
    edges.forEach(e => { if (e.a === id || e.b === id) { keep.add(e.a); keep.add(e.b); } });
    svgEl.querySelectorAll("g.n").forEach(g => {
      g.classList.toggle("dim", !!id && !keep.has(g.dataset.id));
      $("text", g).setAttribute("visibility", !id ? (byId[g.dataset.id].mentions >= labelMin ? "visible" : "hidden") : keep.has(g.dataset.id) ? "visible" : "hidden");
    });
    svgEl.querySelectorAll("line").forEach(l => {
      const e = edges[l.dataset.i]; const on = id && (e.a === id || e.b === id);
      l.classList.toggle("hi", !!on); l.classList.toggle("dim", !!id && !on);
    });
  };
  svgEl.addEventListener("mouseover", e => { const g = e.target.closest("g.n"); highlight(g ? g.dataset.id : null); });
  svgEl.addEventListener("focusin", e => { const g = e.target.closest("g.n"); if (g) highlight(g.dataset.id); });
  svgEl.addEventListener("mouseleave", () => highlight(null));
  const ask = g => {
    const n = byId[g.dataset.id];
    const tpl = (state.L.followup_templates || {})[n.type] || "{name}";
    d.close(); send(fmt(tpl, { name: n.name }));
  };
  svgEl.addEventListener("click", e => { const g = e.target.closest("g.n"); if (g) ask(g); });
  svgEl.addEventListener("keydown", e => { const g = e.target.closest("g.n"); if (g && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); ask(g); } });
}

function openLead(brand) {
  const d = $("#lead-dialog");
  if (brand) $("#lead-brand").value = brand;
  $("#lead-msg").textContent = "";
  d.showModal();
}

async function submitLead(ev) {
  ev.preventDefault();
  const f = ev.target, fd = new FormData(f);
  const payload = Object.fromEntries(fd.entries());
  payload.consent = fd.get("consent") === "on";
  payload.lang = state.lang;
  payload.summary = state.history.filter(t => t.role === "user").map(t => t.content).slice(-4).join(" | ").slice(0, 900);
  const r = await fetch("/api/lead", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).catch(() => null);
  if (r && r.ok) { $("#lead-msg").textContent = ""; f.reset(); $("#lead-dialog").close(); addBotText(state.L.lead_thanks); }
  else $("#lead-msg").textContent = state.L.lead_invalid;
}

function addBotText(t) { const li = addBot(); const b = $(".body", li); b.classList.remove("typing"); b.innerHTML = render(t); }

function renderAll() { renderStats(); renderBrands(); renderHosting(); loadSuggestions(); }

async function boot() {
  const ta = $("#composer-input");
  $("#composer").addEventListener("submit", e => { e.preventDefault(); const v = ta.value; ta.value = ""; ta.style.height = "auto"; send(v); });
  ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; });
  $("#lang-toggle").onclick = async () => { await loadLocale(state.lang === "ar" ? "en" : "ar"); if (state.cfg) renderAll(); };
  $("#new-chat").onclick = () => { state.history = []; $("#thread").innerHTML = ""; $("#intro").hidden = false; loadSuggestions(); window.scrollTo({ top: 0, behavior: "smooth" }); };
  $("#privacy-btn").onclick = () => $("#privacy-dialog").showModal();
  $("#about-btn").onclick = () => $("#about-dialog").showModal();
  $("#graph-btn").onclick = openGraph;
  $("#lead-form").addEventListener("submit", submitLead);
  $("#lead-cancel").onclick = () => $("#lead-dialog").close();
  document.addEventListener("click", e => {
    const b = e.target.closest(".lead-open"); if (b) openLead(b.dataset.brand);
    const f = e.target.closest(".followups button"); if (f) send(f.dataset.q);
  });
  if (!store.get("consent")) { $("#consent").hidden = false; }
  $("#consent-ok").onclick = () => { store.set("consent", "1"); $("#consent").hidden = true; };

  await loadLocale(state.lang);
  await waitReady();
  state.cfg = await (await fetch("/api/config")).json();
  applyTheme(state.cfg.theme);
  $("#wordmark").textContent = state.cfg.wordmark;
  $("#monogram").textContent = state.cfg.monogram || "";
  renderAll();
}

boot();
