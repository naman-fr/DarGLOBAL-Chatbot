"use strict";
// Small dependency-free SPA: SSE chat over fetch, EN/AR with RTL, citations, cards, lead form.

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

function brandClass(key) {
  const brands = (state.cfg?.brands || []).filter(b => b.role !== "parent");
  const b = (state.cfg?.brands || []).find(x => x.key === key);
  if (b && b.role === "parent") return "tag parent";
  return brands.findIndex(x => x.key === key) === 1 ? "tag b1" : "tag";
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
    let inner = (bullet ? line.replace(/^[-*•]\s+/, "") : line)
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
  document.title = state.L.app_title;
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

function renderBrands() {
  const row = $("#brand-row");
  row.innerHTML = "";
  for (const b of state.cfg.brands) {
    const el = document.createElement("span");
    el.className = brandClass(b.key);
    el.innerHTML = `<a href="${esc(b.url)}" target="_blank" rel="noopener">${esc(brandName(b))}</a>`;
    row.append(el);
  }
  const sel = $("#lead-brand");
  sel.innerHTML = state.cfg.brands.filter(b => b.role !== "parent")
    .map(b => `<option value="${esc(b.key)}">${esc(brandName(b))}</option>`).join("");
}

async function loadSuggestions() {
  const box = $("#suggestions");
  box.innerHTML = "";
  try {
    const r = await fetch(`/api/suggestions?lang=${state.lang}`);
    const { items } = await r.json();
    for (const s of items) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip"; b.setAttribute("role", "listitem");
      b.innerHTML = `${esc(s.text)}<small>${esc(s.brand_name)}</small>`;
      b.onclick = () => send(s.text);
      box.append(b);
    }
  } catch { /* suggestions are optional */ }
}

async function waitReady() {
  const wake = $("#waking");
  for (let i = 0; i < 90; i++) {
    try {
      const r = await fetch("/readyz", { cache: "no-store" });
      if (r.ok) {
        const j = await r.json();
        wake.hidden = true;
        $("#kb-version").textContent = j.kb_version ? `· KB ${j.kb_version}` : "";
        return true;
      }
    } catch { /* still waking */ }
    wake.hidden = false;
    await new Promise(r => setTimeout(r, 2000));
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
  const items = cites.map(c => `<li><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>
    <span class="meta">— ${esc(c.brand_name)} · ${esc(c.source || "")} · ${esc(state.L.as_of)} ${esc((c.fetched_at || "").slice(0, 10))}</span></li>`).join("");
  return `<details class="sources"><summary>${esc(state.L.sources)} (${cites.length})</summary><ol>${items}</ol></details>`;
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
      <span class="muted">${esc(L.updated)}: ${esc((x.updated_at || x.fetched_at || "").slice(0, 10))}</span>
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
  let answer = "", cites = [], model = null, brands = new Set();

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
        else if (ev === "citations") { cites = data; data.forEach(c => brands.add(JSON.stringify([c.brand, c.brand_name]))); body.innerHTML = render(answer, cites); }
        else if (ev === "listings") { brands.add(JSON.stringify([data.brand, data.brand_name])); extra.insertAdjacentHTML("beforeend", cardsBlock(data.items)); }
        else if (ev === "handoff") { extra.insertAdjacentHTML("beforeend", handoffBlock(data)); }
        else if (ev === "notice") { extra.insertAdjacentHTML("beforeend", `<p class="notice">${esc(data.text)}</p>`); }
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
  if (cites.some(c => c.source !== "listing")) extra.insertAdjacentHTML("beforeend", sourcesBlock(cites));
  extra.insertAdjacentHTML("beforeend", `<div class="fb"><button type="button" data-r="1">👍 ${esc(state.L.feedback_up)}</button><button type="button" data-r="-1">👎 ${esc(state.L.feedback_down)}</button>${model ? `<span class="model">${esc(state.L.model)}: ${esc(model)}</span>` : ""}</div>`);
  $(".fb", extra).addEventListener("click", async ev => {
    const btn = ev.target.closest("button[data-r]");
    if (!btn) return;
    await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating: Number(btn.dataset.r), question: text, model: model || "" }) }).catch(() => {});
    $(".fb", extra).innerHTML = `<span>${esc(state.L.feedback_thanks)}</span>`;
  });
  state.history.push({ role: "user", content: text }, { role: "assistant", content: answer.slice(0, 1500) });
  state.busy = false; $("#send").disabled = false;
  li.scrollIntoView({ block: "end", behavior: "smooth" });
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

async function boot() {
  const ta = $("#composer-input");
  $("#composer").addEventListener("submit", e => { e.preventDefault(); const v = ta.value; ta.value = ""; send(v); });
  ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; });
  $("#lang-toggle").onclick = async () => { await loadLocale(state.lang === "ar" ? "en" : "ar"); renderBrands(); loadSuggestions(); };
  $("#new-chat").onclick = () => { state.history = []; $("#thread").innerHTML = ""; $("#intro").hidden = false; loadSuggestions(); };
  $("#privacy-btn").onclick = () => $("#privacy-dialog").showModal();
  $("#lead-form").addEventListener("submit", submitLead);
  $("#lead-cancel").onclick = () => $("#lead-dialog").close();
  document.addEventListener("click", e => { const b = e.target.closest(".lead-open"); if (b) openLead(b.dataset.brand); });
  if (!store.get("consent")) { $("#consent").hidden = false; }
  $("#consent-ok").onclick = () => { store.set("consent", "1"); $("#consent").hidden = true; };

  await loadLocale(state.lang);
  await waitReady();
  state.cfg = await (await fetch("/api/config")).json();
  applyTheme(state.cfg.theme);
  $("#wordmark").textContent = state.cfg.wordmark;
  renderBrands();
  loadSuggestions();
}

boot();
