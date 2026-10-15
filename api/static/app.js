"use strict";
// CASCADE dashboard. Keys live only in memory (never storage, never URLs).
const state = { ph: "", reg: "", streams: {}, fields: null, alerts: new Map(), current: null,
                chart: null, chartPair: null, newPairs: new Set(), lastSignals: 0, demoTimer: null };
const $ = (id) => document.getElementById(id);
const H1 = "C09XX>A02BC";

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k);
  return e;
}
const fmt = (x, d = 1) => (x == null ? "—" : x >= 999 ? "∞" : Number(x).toFixed(d));

async function api(path, key, opts = {}) {
  const r = await fetch(path, { signal: AbortSignal.timeout(opts.timeout || 20000), ...opts,
                                headers: { "X-API-Key": key, "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 160)}`);
  return r.json();
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 6000);
}

// ---------------------------------------------------------------- SSE over fetch (header auth)
async function openStream(name, key, onEvent) {
  if (state.streams[name]) state.streams[name].abort();
  const ctl = new AbortController(); state.streams[name] = ctl;
  let delay = 1000;
  while (!ctl.signal.aborted) {
    try {
      const r = await fetch("/stream", { headers: { "X-API-Key": key }, signal: ctl.signal });
      if (!r.ok) throw new Error(String(r.status));
      delay = 1000;
      const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2);
          let ev = "message", data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event: ")) ev = line.slice(7);
            else if (line.startsWith("data: ")) data += line.slice(6);
          }
          if (data) { try { onEvent(ev, JSON.parse(data)); } catch (e) { console.warn(e); } }
        }
      }
    } catch (e) { if (ctl.signal.aborted) return; }
    await new Promise((res) => setTimeout(res, delay)); delay = Math.min(delay * 2, 15000);
  }
}

// ---------------------------------------------------------------- top strip
async function pollMetrics() {
  const key = state.ph || state.reg;
  if (!key || pollMetrics.busy) return;
  pollMetrics.busy = true;
  try {
    const m = await api("/metrics", key);
    $("k-eps").textContent = m.events_per_s == null ? "—" : m.events_per_s.toLocaleString();
    $("k-p95").textContent = m.latency_ms ? `${m.latency_ms.p95.toFixed(1)} ms` : "—";
    $("k-arch").textContent = m.arch || "—";
    $("k-enc").textContent = m.encryption.at_rest ? `${m.encryption.at_rest}${m.encryption.tls ? " + TLS" : ""}` : "—";
    $("k-val").textContent = m.validator_pass_rate == null ? "—" : `${(m.validator_pass_rate * 100).toFixed(0)}%`;
    $("k-sim").textContent = m.loop2_sim_date || "—";
  } catch (e) { /* keep last values */ } finally { pollMetrics.busy = false; }
}

// ---------------------------------------------------------------- pharmacist
function feedItem(ev) {
  const ul = $("feed");
  ul.prepend(el("li", {}, el("span", { text: ev.drug }), el("span", { class: "t", text: ev.ts.slice(0, 16).replace("T", " ") })));
  while (ul.children.length > 40) ul.lastChild.remove();
}
function badgeFor(a) {
  if (a.known_source) return el("span", { class: "badge", text: `Known: ${a.known_source}` });
  return el("span", { class: "badge new", text: a.pair_status === "corroborated" ? "Population signal · FAERS" : "Population signal" });
}
function renderCard(a) {
  const card = el("article", { class: `card ${a.status || "open"}`, "data-id": a.alert_id, "aria-label": `Alert ${a.alert_id}` },
    el("div", { class: "row1" }, el("span", { class: "drugs", text: `${a.index_drug} → ${a.marker_drug}` }),
       el("span", { class: "score", text: `score ${Number(a.score).toFixed(2)}` })),
    el("div", { class: "muted small", text: `${a.marker_drug} started ${a.days_between} days after ${a.index_drug} · ${a.ts.slice(0, 10)} · ${a.alert_id}` }),
    el("ul", {}, ...a.reasons.map((r) => el("li", { text: r }))));
  const btn = el("button", { type: "button", class: "primary", text: a.status === "approved" ? "View draft" : "Draft note" });
  btn.addEventListener("click", () => openDraft(a.alert_id, btn));
  card.append(el("div", { class: "foot" }, el("span", {}, badgeFor(a), " ",
    el("span", { class: "badge status", text: a.status || "open" })), btn));
  return card;
}
function addAlert(a, prepend = true) {
  const box = $("alerts");
  box.querySelector(".empty")?.remove();
  const old = box.querySelector(`[data-id="${a.alert_id}"]`);
  const card = renderCard(a);
  if (old) old.replaceWith(card); else if (prepend) box.prepend(card); else box.append(card);
  state.alerts.set(a.alert_id, a);
  $("alert-count").textContent = `(${state.alerts.size})`;
}
async function connectPharmacist() {
  const list = await api("/alerts?limit=30", state.ph);
  $("alerts").replaceChildren(el("p", { class: "empty", text: "No alerts yet." }));
  state.alerts.clear();
  list.reverse().forEach((a) => addAlert(a));
  $("ph-who").textContent = "connected";
  openStream("ph", state.ph, (ev, d) => {
    if (ev === "hello") $("ph-who").textContent = d.role;
    else if (ev === "event") feedItem(d);
    else if (ev === "alert") { addAlert({ ...d, status: "open" }); toast(`Possible cascade: ${d.index_drug} → ${d.marker_drug}`); }
  });
}

function renderForm(report) {
  const box = $("dr-form"); box.replaceChildren();
  const tbc = "To be completed by reporter";
  for (const sec of state.fields.sections) {
    const val = report[sec.key];
    const items = Array.isArray(val) ? (val.length ? val : [{}]) : [val || {}];
    items.forEach((item, n) => {
      box.append(el("h4", { text: sec.title + (items.length > 1 ? ` (${n + 1})` : "") }));
      const tb = el("tbody");
      for (const f of sec.fields) {
        const v = item ? item[f.key] : null;
        const empty = v == null || v === "";
        tb.append(el("tr", {}, el("th", { scope: "row", text: f.label }),
                     el("td", { class: empty ? "tbc" : "", text: empty ? tbc : String(v) })));
      }
      box.append(el("table", {}, tb));
    });
  }
}
async function openDraft(id, opener) {
  if (!state.fields) state.fields = await (await fetch("/pvpi/fields")).json();
  state.current = { id, opener };
  $("dr-title").textContent = `Draft for ${id}`;
  $("dr-note").textContent = "Generating…"; $("dr-form").replaceChildren(); $("dr-meta").textContent = "";
  $("dismiss-box").hidden = true;
  $("drawer").hidden = false; $("dr-close").focus();
  try {
    const detail = await api(`/alerts/${encodeURIComponent(id)}`, state.ph);  // audited as alert_view
    const d = await api(`/alerts/${encodeURIComponent(id)}/draft`, state.ph, { method: "POST", timeout: 30000 });
    $("dr-note").textContent = d.note_text;
    $("dr-meta").textContent = `Generator: ${d.generator.startsWith("llm") ? "LLM (" + d.generator.slice(4) + ")" : d.generator}` +
      ` · validator ${d.validator_pass ? "passed" : "FAILED"} · ${d.gen_ms} ms · external data: ${detail.evidence.faers}`;
    renderForm(d.report);
    const a = state.alerts.get(id);
    if (a && a.status === "open") addAlert({ ...a, status: "drafted" }, false);
    $("dr-approve").focus();
  } catch (e) { $("dr-note").textContent = `Could not create draft: ${e.message}`; }
}
function closeDrawer() {
  $("drawer").hidden = true;
  state.current?.opener?.focus?.();
  state.current = null;
}
function download(name, type, data) {
  const blob = data instanceof Blob ? data : new Blob([data], { type });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: name }); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
async function approve() {
  const id = state.current?.id; if (!id) return;
  $("dr-approve").disabled = true;
  try {
    const r = await api(`/alerts/${encodeURIComponent(id)}/approve`, state.ph, { method: "POST" });
    const bytes = Uint8Array.from(atob(r.file_base64), (c) => c.charCodeAt(0));
    download(r.file_name, r.file_type, new Blob([bytes], { type: r.file_type }));
    download(`${id}_pvpi_draft.json`, "application/json", JSON.stringify({ alert_id: id, note: r.note, report: r.report,
      generator: r.generator, validator_pass: r.validator_pass, approved_ts: r.approved_ts }, null, 2));
    const a = state.alerts.get(id); if (a) addAlert({ ...a, status: "approved" }, false);
    toast(`Approved ${id}: PDF and JSON exported`);
    closeDrawer();
  } catch (e) { toast(`Approve failed: ${e.message}`); } finally { $("dr-approve").disabled = false; }
}
async function dismiss() {
  const id = state.current?.id; const reason = $("dismiss-reason").value.trim();
  if (!id || !reason) { $("dismiss-reason").focus(); return; }
  try {
    await api(`/alerts/${encodeURIComponent(id)}/dismiss`, state.ph, { method: "POST", body: JSON.stringify({ reason }) });
    const a = state.alerts.get(id); if (a) addAlert({ ...a, status: "dismissed" }, false);
    $("dismiss-reason").value = ""; closeDrawer();
  } catch (e) { toast(`Dismiss failed: ${e.message}`); }
}

// ---------------------------------------------------------------- regulator
const pairKey = (s) => `${s.a.atc_l4}>${s.b.atc_l4}`;
function classCell(c) {
  return el("span", {}, c.atc_l4, el("div", { class: "ex", text: c.examples.join(", ") + (c.fictional ? " (fictional)" : "") }));
}
async function loadSignals() {
  if (!state.reg) return;
  const w = $("win").value, bh = $("bh").checked ? 1 : 0;
  const r = await api(`/signals?window=${w}&bh=${bh}`, state.reg);
  $("sig-summary").textContent = `${r.n_signals} signals (${r.n_signals_bh} after BH) among ${r.tested_pairs} pairs with data` +
    (r.sim_date ? ` · as of ${r.sim_date}` : "") + " · counts below 5 shown as <5";
  const tb = $("sig-rows"); tb.replaceChildren();
  for (const s of r.signals) {
    const k = pairKey(s);
    const tr = el("tr", { tabindex: "0", "aria-label": `${k} aSR ${fmt(s.asr)}` },
      el("td", {}, el("strong", {}, classCell(s.a)), " → ", classCell(s.b)),
      el("td", { text: `${fmt(s.asr)} [${fmt(s.lo)}–${fmt(s.hi)}]` }),
      el("td", { text: String(s.n) }),
      el("td", { class: "small", text: s.first_signal_date || "—" }),
      el("td", {}, el("span", { class: `chip ${s.status}`, text: s.status })));
    if (k === H1 || (s.status === "new" && s.is_signal_bh)) tr.classList.add("h1");  // BH-confirmed new signal
    if (state.newPairs.has(k)) tr.classList.add("flash");
    if (k === state.chartPair) tr.classList.add("sel");
    const open = () => showHistory(s.a.atc_l4, s.b.atc_l4, `${s.a.atc_l4} → ${s.b.atc_l4} (${s.a.examples[0] || ""} → ${s.b.examples[0] || ""})`);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    tb.append(tr);
  }
  if (!r.signals.length) tb.append(el("tr", {}, el("td", { colspan: "5", class: "muted", text: "No signals yet." })));
}
async function showHistory(a, b, title) {
  const w = $("win").value;
  const r = await api(`/signals/${a}/${b}/history?window=${w}`, state.reg);
  state.chartPair = `${a}>${b}`;
  $("chart-wrap").hidden = false; $("chart-title").textContent = `${title} · W=${w}`;
  const clamp = (v) => (v == null ? null : Math.min(Math.max(v, 0.05), 200));
  const labels = r.points.map((p) => p.sim_date);
  const data = {
    labels,
    datasets: [
      { label: "lower 95%", data: r.points.map((p) => clamp(p.lo)), borderWidth: 0, pointRadius: 0, fill: false },
      { label: "95% CI", data: r.points.map((p) => clamp(p.hi)), borderWidth: 0, pointRadius: 0,
        backgroundColor: "rgba(31,95,191,0.15)", fill: "-1" },
      { label: "aSR", data: r.points.map((p) => clamp(p.asr)), borderColor: "#1f5fbf", borderWidth: 2, pointRadius: 0, fill: false },
      { label: "null (aSR = 1)", data: labels.map(() => 1), borderColor: "#5d6877", borderDash: [5, 4], borderWidth: 1, pointRadius: 0, fill: false },
    ],
  };
  if (!window.Chart) { $("chart-title").textContent += " (Chart.js not loaded)"; return; }
  if (state.chart) state.chart.destroy();
  state.chart = new Chart($("chart"), { type: "line", data,
    options: { animation: false, responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { y: { type: "logarithmic", title: { display: true, text: "aSR (log scale, capped at 200)" } },
                x: { ticks: { maxTicksLimit: 8 } } },
      plugins: { legend: { labels: { boxWidth: 12, filter: (i) => i.text !== "lower 95%" } } } } });
  document.querySelectorAll("#sig-rows tr").forEach((tr) => tr.classList.remove("sel"));
}
function connectRegulator() {
  loadSignals().catch((e) => { $("sig-summary").textContent = `Error: ${e.message}`; });
  openStream("reg", state.reg, (ev, d) => {
    if (ev !== "signals") return;
    for (const s of d.new_signals || []) {
      const k = `${s.a}>${s.b}`;
      // toast only BH-confirmed new signals: raw CI-rule crossings of small chance pairs are frequent
      if (s.status === "new" && s.bh && !state.newPairs.has(k)) {
        state.newPairs.add(k); toast(`New signal: not on any known list (${s.a} → ${s.b})`);
      }
    }
    const now = Date.now();
    if ((d.new_signals || []).length || now - state.lastSignals > 3000) {
      state.lastSignals = now; loadSignals().catch(() => {});
      if (state.chartPair) { const [a, b] = state.chartPair.split(">"); showHistory(a, b, $("chart-title").textContent.split(" · ")[0]).catch(() => {}); }
    }
  });
}

// ---------------------------------------------------------------- demo
async function checkDemo() {
  const key = state.ph || state.reg;
  try {
    await api("/admin/demo/status", key); $("demo-btn").hidden = false;
    clearInterval(state.demoTimer); state.demoTimer = setInterval(pollDemo, 2000); pollDemo();
  } catch (e) { $("demo-btn").hidden = true; }
}
async function pollDemo() {
  if (pollDemo.busy) return;  // never stack requests; a stuck one times out (see api())
  pollDemo.busy = true;
  try {
    const s = await api("/admin/demo/status", state.ph || state.reg, { timeout: 5000 });
    $("demo-status").textContent = s.stage === "idle" ? "" : `demo: ${s.stage}${s.current_ts ? " · " + s.current_ts.slice(0, 10) : ""}`;
  } catch (e) { /* keep last text */ } finally { pollDemo.busy = false; }
}

// ---------------------------------------------------------------- wiring
function applyView() {
  const v = document.querySelector("input[name=view]:checked").value;
  $("grid").className = "grid" + (v === "pharmacist" ? " only-ph" : v === "regulator" ? " only-reg" : "");
}
document.addEventListener("DOMContentLoaded", () => {
  $("keys").addEventListener("submit", async (e) => {
    e.preventDefault();
    state.ph = $("key-ph").value.trim(); state.reg = $("key-reg").value.trim();
    $("key-ph").value = ""; $("key-reg").value = "";  // keep only in JS memory
    if (state.ph) connectPharmacist().catch((err) => { $("ph-who").textContent = `error: ${err.message}`; });
    if (state.reg) connectRegulator();
    pollMetrics(); checkDemo();
  });
  document.querySelectorAll("input[name=view]").forEach((r) => r.addEventListener("change", applyView));
  $("win").addEventListener("change", () => { $("chart-wrap").hidden = true; state.chartPair = null; loadSignals().catch(() => {}); });
  $("bh").addEventListener("change", () => loadSignals().catch(() => {}));
  $("chart-close").addEventListener("click", () => { $("chart-wrap").hidden = true; state.chartPair = null; });
  $("dr-close").addEventListener("click", closeDrawer);
  $("dr-approve").addEventListener("click", approve);
  $("dr-dismiss").addEventListener("click", () => { $("dismiss-box").hidden = false; $("dismiss-reason").focus(); });
  $("dismiss-confirm").addEventListener("click", dismiss);
  $("demo-btn").addEventListener("click", async () => {
    try { await api("/admin/demo/start", state.ph || state.reg, { method: "POST" }); toast("Demo script started"); }
    catch (e) { toast(`Demo start failed: ${e.message}`); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) closeDrawer(); });
  setInterval(pollMetrics, 2000);
});
