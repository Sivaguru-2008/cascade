"use strict";

/**
 * CASCADE — Real-Time Prescribing Cascade Detection & Pharmacovigilance
 * Production Single Page Application Engine
 * IBM Z Datathon 2026 · Team OVERCLOCKED
 */

// Official Zero-Friction Demonstration Keys
const DEMO_PH_KEY = "ph30_fe0d57b8a964b958a6aafff71d71232f58a73d53";
const DEMO_REG_KEY = "reg_87ddc0286d4d577aeb4500511be2f97272ee55ad";

// Application State
const state = {
  role: "pharmacist", // "pharmacist" or "regulator"
  phKey: DEMO_PH_KEY,
  regKey: DEMO_REG_KEY,
  activeRoute: "overview",
  streams: {},
  fields: null,
  alerts: new Map(),
  recentEvents: [],
  signals: [],
  reports: [],
  auditLogs: [],
  metrics: null,
  selectedAlertId: null,
  selectedSignalPair: null,
  chartInstance: null,
  demoTimer: null,
  demoStage: "idle",
  isSidebarCollapsed: false,
  // Clinical EHR & Patient Portal State
  selectedDoctorId: null,
  selectedPatientId: "P001142",
  doctors: [],
  patients: [],
  clinicalTimeline: null,
  currentPrescriptions: [],
  availableDrugs: [],
  reactions: [],
  precheckDebounceTimer: null,
};

// Stage configurations for guided demonstration
const DEMO_STAGES = {
  idle: { num: 0, title: "Simulation Idle", desc: "Ready to launch live multi-stage simulation." },
  warmup: { num: 1, title: "Phase 1: Historical Baseline Warm-up", desc: "Replaying historical dispenses at max speed to establish patient baselines across 50 pharmacies." },
  warmed_up: { num: 1, title: "Phase 1 Complete: System Warmed Up", desc: "Baselines established. Streaming real-time dispensing events." },
  k1_cascade: { num: 2, title: "Phase 2: Prescribing Cascade Intercepted!", desc: "Amlodipine ➔ Furosemide cascade intercepted at Pharmacy PH030! Sub-15ms ML flagged the event with TreeSHAP reasons." },
  to_coincidental: { num: 2, title: "Advancing to Coincidence Test", desc: "Replaying stream toward known class pair..." },
  coincidental: { num: 3, title: "Phase 3: Coincidence Discrimination Tested", desc: "Diclofenac ➔ Amlodipine dispensing evaluated and scored below threshold (proving low false-alarm rate)." },
  novacor_fast_forward: { num: 4, title: "Phase 4: Market Uptake of Novel Drug 'Novacor'", desc: "Commercial uptake of fictional drug Novacor surging across pharmacies to test unsupervised signal emergence..." },
  h1_flagged: { num: 5, title: "Phase 5: Signal Discovered! Novel Cascade Uncovered", desc: "Loop 2 Streaming PSSA detected Novacor ➔ PPI as an unlisted novel prescribing cascade across all pharmacies!" },
  done: { num: 5, title: "Simulation Complete", desc: "All phases finished. Review alerts, drafts, and population signals." },
  error: { num: 0, title: "Simulation Error", desc: "Replay encountered an error or demo plan is missing." }
};

// Helper: DOM selector
const $ = (id) => document.getElementById(id);

// Helper: Format numbers
const fmt = (x, d = 1) => (x == null ? "—" : x >= 999 ? "∞" : Number(x).toFixed(d));

// Helper: Create element with attributes & children
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("data-")) e.setAttribute(k, v);
    else if (k in e && typeof e[k] !== "function") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const k of kids) {
    if (k != null) {
      if (typeof k === "string" || typeof k === "number") e.append(document.createTextNode(String(k)));
      else e.append(k);
    }
  }
  return e;
}

// ---------------------------------------------------------------------------
// 1. Central API Layer
// ---------------------------------------------------------------------------

function currentKey() {
  return state.role === "pharmacist" ? state.phKey : state.regKey;
}

async function api(path, key = null, opts = {}) {
  const apiKey = key || currentKey();
  const url = path.startsWith("http") ? path : path;
  const timeoutMs = opts.timeout || 25000;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, {
      ...opts,
      signal: controller.signal,
      headers: {
        "X-API-Key": apiKey,
        "Content-Type": "application/json",
        ...(opts.headers || {})
      }
    });

    if (!res.ok) {
      let errorBody = "";
      try {
        errorBody = await res.text();
      } catch (_) {}
      const msg = `API Error ${res.status}: ${errorBody.slice(0, 160) || res.statusText}`;
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }

    const contentType = res.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      return await res.json();
    }
    return await res.text();
  } finally {
    clearTimeout(timeoutId);
  }
}

// Toast notification helper
function showToast(message, isCritical = false) {
  const toast = $("app-toast");
  if (!toast) return;
  toast.textContent = message;
  toast.style.borderLeftColor = isCritical ? "var(--color-critical)" : "var(--brand-primary)";
  toast.hidden = false;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { toast.hidden = true; }, 5000);
}

// ---------------------------------------------------------------------------
// 2. Server-Sent Events (SSE) Stream Manager
// ---------------------------------------------------------------------------

async function openEventStream(name, key, onEvent) {
  if (state.streams[name]) {
    state.streams[name].abort();
  }
  const ctl = new AbortController();
  state.streams[name] = ctl;
  let delay = 1000;

  const updateStatus = (connected, text) => {
    const pill = $("connection-status-pill");
    const label = $("connection-status-text");
    if (pill && label) {
      if (connected) {
        pill.className = "connection-badge connected";
        label.textContent = text || "Connected";
      } else {
        pill.className = "connection-badge disconnected";
        label.textContent = text || "Reconnecting...";
      }
    }
  };

  while (!ctl.signal.aborted) {
    try {
      updateStatus(false, "Connecting...");
      const response = await fetch("/stream", {
        headers: { "X-API-Key": key },
        signal: ctl.signal
      });

      if (!response.ok) {
        throw new Error(`SSE HTTP ${response.status}`);
      }

      updateStatus(true, "Connected");
      delay = 1000;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let pos;
        while ((pos = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, pos);
          buffer = buffer.slice(pos + 2);
          let eventType = "message";
          let dataText = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event: ")) eventType = line.slice(7).trim();
            else if (line.startsWith("data: ")) dataText += line.slice(6);
          }
          if (dataText) {
            try {
              const parsed = JSON.parse(dataText);
              onEvent(eventType, parsed);
            } catch (err) {
              console.warn("Failed to parse SSE JSON:", err, dataText);
            }
          }
        }
      }
    } catch (err) {
      if (ctl.signal.aborted) return;
      updateStatus(false, "Disconnected");
    }
    await new Promise((res) => setTimeout(res, delay));
    delay = Math.min(delay * 2, 15000);
  }
}

// ---------------------------------------------------------------------------
// 3. Client-Side Hash Router
// ---------------------------------------------------------------------------

function handleRouting() {
  const hash = window.location.hash.slice(1) || "/overview";
  const parts = hash.split("/").filter(Boolean);
  const route = parts[0] || "overview";
  const param = parts[1] || null;

  navigateTo(route, param, false);
}

function navigateTo(route, param = null, updateHash = true) {
  state.activeRoute = route;

  // Update hash if requested
  if (updateHash) {
    window.location.hash = param ? `#/${route}/${param}` : `#/${route}`;
  }

  // Update navigation links
  document.querySelectorAll(".sidebar-nav .nav-link").forEach((link) => {
    if (link.getAttribute("data-route") === route) {
      link.classList.add("active");
    } else {
      link.classList.remove("active");
    }
  });

  // Update breadcrumb
  const breadcrumb = $("topbar-breadcrumb");
  if (breadcrumb) {
    const titles = {
      overview: "Overview",
      monitoring: "Live Monitoring & Pipeline",
      alerts: "Cascade Alerts",
      signals: "Population Signals (Loop 2 PSSA)",
      reports: "Reports & PvPI",
      audit: "System Audit Trail",
      system: "System Status & Telemetry",
      doctor: "Doctor Consultation Station (EHR)",
      patient: "MyHealth Patient Portal"
    };
    breadcrumb.textContent = titles[route] || route.toUpperCase();
  }

  // Show/hide view containers
  document.querySelectorAll(".app-view").forEach((view) => {
    if (view.id === `view-${route}`) {
      view.classList.add("active");
    } else {
      view.classList.remove("active");
    }
  });

  // Route-specific loader
  if (route === "overview") {
    loadOverview();
  } else if (route === "monitoring") {
    loadMonitoring();
  } else if (route === "alerts") {
    loadAlerts(param);
  } else if (route === "signals") {
    loadSignalsView(param);
  } else if (route === "reports") {
    loadReportsView();
  } else if (route === "audit") {
    loadAuditView();
  } else if (route === "system") {
    loadSystemView();
  } else if (route === "doctor") {
    loadDoctorStation();
  } else if (route === "patient") {
    loadPatientPortal();
  }
}

// ---------------------------------------------------------------------------
// 4. Telemetry KPIs & Background Refresh
// ---------------------------------------------------------------------------

async function refreshMetrics() {
  try {
    const m = await api("/metrics", currentKey(), { timeout: 6000 });
    state.metrics = m;

    // Topbar KPIs
    if ($("topbar-eps")) $("topbar-eps").textContent = m.events_per_s != null ? m.events_per_s.toLocaleString() : "—";
    if ($("topbar-p95")) $("topbar-p95").textContent = m.latency_ms ? `${m.latency_ms.p95.toFixed(1)} ms` : "—";
    if ($("topbar-arch")) $("topbar-arch").textContent = m.arch || "s390x / LinuxONE";
    if ($("topbar-val")) $("topbar-val").textContent = m.validator_pass_rate != null ? `${(m.validator_pass_rate * 100).toFixed(0)}%` : "—";
    if ($("topbar-sim")) $("topbar-sim").textContent = m.loop2_sim_date ? m.loop2_sim_date.slice(0, 10) : "—";

    // Overview KPIs
    if ($("ov-metric-scored")) $("ov-metric-scored").textContent = (m.events_scored_total || 0).toLocaleString();
    if ($("ov-metric-alerts")) $("ov-metric-alerts").textContent = String(m.alerts_total ?? "—");
    if ($("ov-metric-open")) $("ov-metric-open").textContent = String(m.alerts?.open ?? "0");
    if ($("ov-metric-drafts")) $("ov-metric-drafts").textContent = String(m.drafts_total ?? "0");

    // Monitoring Latencies
    if ($("mon-lat-p50")) $("mon-lat-p50").textContent = m.latency_ms ? `${m.latency_ms.p50.toFixed(2)} ms` : "—";
    if ($("mon-lat-p95")) $("mon-lat-p95").textContent = m.latency_ms ? `${m.latency_ms.p95.toFixed(2)} ms` : "—";
    if ($("mon-lat-p99")) $("mon-lat-p99").textContent = m.latency_ms ? `${m.latency_ms.p99.toFixed(2)} ms` : "—";
    if ($("mon-lat-sample")) $("mon-lat-sample").textContent = m.latency_ms ? m.latency_ms.sample.toLocaleString() : "—";

    // System Telemetry View updates
    if ($("sys-enc-rest")) $("sys-enc-rest").textContent = m.encryption?.at_rest ? `${m.encryption.at_rest}` : "AES-256-GCM";
    if ($("sys-enc-tls")) $("sys-enc-tls").textContent = m.encryption?.tls ? "TLS Enforced" : "HTTP (Local Dev Mode)";
    if ($("sys-db-badge")) $("sys-db-badge").textContent = m.db ? `${m.db.toUpperCase()} (Dialect)` : "SQLite";
  } catch (err) {
    // Gracefully preserve telemetry
  }
}

// ---------------------------------------------------------------------------
// 5. Page 1: Overview Dashboard
// ---------------------------------------------------------------------------

async function loadOverview() {
  refreshMetrics();
  updateActionQueue();
}

function updateActionQueue() {
  const queue = $("ov-action-queue");
  if (!queue) return;
  queue.replaceChildren();

  const openAlerts = Array.from(state.alerts.values()).filter((a) => a.status === "open" || !a.status);
  if (!openAlerts.length) {
    queue.append(
      el("div", { class: "action-item" },
        el("div", { class: "action-left" },
          el("span", { class: "action-badge-dot dot-blue" }),
          el("div", { class: "action-text-group" },
            el("span", { class: "action-title", text: "No pending cascade alerts" }),
            el("span", { class: "action-detail", text: "All alerts reviewed or no alerts currently detected." })
          )
        )
      )
    );
    return;
  }

  openAlerts.slice(0, 5).forEach((alert) => {
    const item = el("div", { class: "action-item" },
      el("div", { class: "action-left" },
        el("span", { class: "action-badge-dot dot-critical" }),
        el("div", { class: "action-text-group" },
          el("span", { class: "action-title", text: `Review Alert #${alert.alert_id}: ${alert.index_drug} ➔ ${alert.marker_drug}` }),
          el("span", { class: "action-detail", text: `Risk Score: ${fmt(alert.score, 2)} · Started ${alert.days_between} days apart` })
        )
      ),
      el("button", {
        type: "button",
        class: "btn btn-sm btn-primary",
        text: "Inspect",
        onclick: () => openAlertModal(alert.alert_id)
      })
    );
    queue.append(item);
  });
}

function recordRecentEvent(ev) {
  state.recentEvents.unshift(ev);
  if (state.recentEvents.length > 50) state.recentEvents.pop();

  // Update Overview Activity Table
  const ovTbody = $("ov-activity-tbody");
  if (ovTbody) {
    const row = el("tr", { class: ev.isAlert ? "highlight-alert" : "" },
      el("td", { text: (ev.ts || "").slice(0, 19).replace("T", " ") }),
      el("td", {}, el("span", { class: `badge ${ev.isAlert ? "badge-open" : "badge-known"}`, text: ev.type || "Dispense" })),
      el("td", { text: ev.drug || "—" }),
      el("td", { text: ev.isAlert ? "🚨 Potential Prescribing Cascade Flagged" : "Routine Prescription Logged" }),
      el("td", {},
        ev.alertId
          ? el("button", { type: "button", class: "btn btn-sm btn-secondary", text: "Review Alert", onclick: () => openAlertModal(ev.alertId) })
          : el("span", { text: "—", style: "color:var(--text-muted);" })
      )
    );
    ovTbody.prepend(row);
    while (ovTbody.children.length > 10) ovTbody.lastChild.remove();
  }

  // Update Monitoring Feed Table
  const monTbody = $("mon-stream-tbody");
  if (monTbody) {
    const filter = $("mon-feed-filter") ? $("mon-feed-filter").value : "all";
    if (filter === "alerts" && !ev.isAlert) return;
    if (filter === "dispenses" && ev.isAlert) return;

    const monRow = el("tr", { class: ev.isAlert ? "highlight-alert" : "" },
      el("td", { text: (ev.ts || "").slice(11, 19) }),
      el("td", { class: "small", text: (ev.event_id || "").slice(0, 8) + "..." }),
      el("td", { text: ev.pharmacy_id || "PH030" }),
      el("td", {}, el("strong", { text: ev.drug || "—" })),
      el("td", { text: ev.atc_l4 || "—" }),
      el("td", {}, el("span", { class: `badge ${ev.isAlert ? "badge-open" : ""}`, text: ev.isAlert ? "Cascade Alert" : "Scored (Routine)" }))
    );
    monTbody.prepend(monRow);
    while (monTbody.children.length > 50) monTbody.lastChild.remove();
  }
}

// ---------------------------------------------------------------------------
// 6. Page 2: Live Monitoring & Demonstration Controller
// ---------------------------------------------------------------------------

async function loadMonitoring() {
  refreshMetrics();
  pollDemoStatus();
}

async function startDemoReplay() {
  try {
    await api("/admin/demo/start", currentKey(), { method: "POST" });
    showToast("🚀 Demonstration replay initiated! Replaying baseline dispenses...", false);
    pollDemoStatus();
  } catch (err) {
    showToast(`Failed to start demo: ${err.message}`, true);
  }
}

async function pollDemoStatus() {
  try {
    const s = await api("/admin/demo/status", currentKey(), { timeout: 5000 });
    state.demoStage = s.stage || "idle";

    const label = $("demo-status-label");
    if (label) {
      const conf = DEMO_STAGES[s.stage] || { num: 1, title: s.stage, desc: "" };
      label.textContent = `Stage: ${conf.title}`;

      // Update 5 stage chips
      for (let i = 1; i <= 5; i++) {
        const chip = $(`stage-chip-${i}`);
        if (chip) {
          chip.className = "demo-stage-chip";
          if (conf.num > i) chip.classList.add("completed");
          else if (conf.num === i) chip.classList.add("active");
        }
      }
    }
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// 7. Page 3: Cascade Alerts & Investigation
// ---------------------------------------------------------------------------

async function loadAlerts(targetAlertId = null) {
  if (state.role !== "pharmacist") {
    const tbody = $("alerts-table-tbody");
    if (tbody) {
      tbody.replaceChildren(
        el("tr", {},
          el("td", { colspan: "8", style: "text-align:center;padding:36px;color:var(--text-secondary);" },
            el("div", { style: "font-weight:700;font-size:15px;margin-bottom:6px;color:var(--text-primary);", text: "Role Restriction: Pharmacist Authentication Required" }),
            el("p", { text: "Prescribing cascade counter alerts contain patient-level intervention context and are restricted to authorized pharmacists. Switch your role to 'Pharmacist' in the top-right header to view alerts." })
          )
        )
      );
    }
    return;
  }

  try {
    const alerts = await api("/alerts?limit=100", state.phKey);
    state.alerts.clear();
    alerts.forEach((a) => state.alerts.set(a.alert_id, a));
    renderAlertsTable();
    updateActionQueue();

    if ($("nav-badge-alerts")) $("nav-badge-alerts").textContent = state.alerts.size;

    if (targetAlertId) {
      openAlertModal(targetAlertId);
    }
  } catch (err) {
    showToast(`Error loading alerts: ${err.message}`, true);
  }
}

function renderAlertsTable() {
  const tbody = $("alerts-table-tbody");
  if (!tbody) return;
  tbody.replaceChildren();

  const search = ($("alert-search")?.value || "").toLowerCase().trim();
  const statusFilter = $("alert-status-filter")?.value || "";

  let list = Array.from(state.alerts.values());
  if (statusFilter) {
    list = list.filter((a) => (a.status || "open") === statusFilter);
  }
  if (search) {
    list = list.filter((a) =>
      (a.index_drug || "").toLowerCase().includes(search) ||
      (a.marker_drug || "").toLowerCase().includes(search) ||
      (a.alert_id || "").toLowerCase().includes(search)
    );
  }

  if (!list.length) {
    tbody.append(
      el("tr", {},
        el("td", { colspan: "8", style: "text-align:center;padding:36px;color:var(--text-muted);" },
          el("div", { style: "font-size:14px;font-weight:600;margin-bottom:4px;", text: "No cascade alerts matching criteria" }),
          el("div", { style: "font-size:12px;", text: "Start the guided simulation or adjust filters to view prescribing cascade alerts." })
        )
      )
    );
    return;
  }

  list.forEach((alert) => {
    const tr = el("tr", { "data-alert-id": alert.alert_id },
      el("td", { class: "font-mono font-bold", text: alert.alert_id }),
      el("td", { class: "small", text: (alert.ts || "").slice(0, 10) }),
      el("td", {},
        el("strong", { text: alert.index_drug || "Drug A" }),
        " ➔ ",
        el("span", { style: "color:var(--color-critical);font-weight:600;", text: alert.marker_drug || "Drug B" })
      ),
      el("td", { text: `${alert.days_between} days` }),
      el("td", {},
        el("span", {
          class: `risk-pill ${alert.score >= 0.7 ? "risk-high" : alert.score >= 0.4 ? "risk-med" : "risk-low"}`,
          text: fmt(alert.score, 2)
        })
      ),
      el("td", {},
        alert.known_source
          ? el("span", { class: "badge badge-known", text: `Literature: ${alert.known_source}` })
          : el("span", { class: "badge badge-novel", text: alert.pair_status === "corroborated" ? "FAERS Corroborated" : "Novel Discovered Signal" })
      ),
      el("td", {},
        el("span", { class: `badge badge-${alert.status || "open"}`, text: alert.status || "open" })
      ),
      el("td", {},
        el("button", {
          type: "button",
          class: "btn btn-sm btn-primary",
          text: alert.status === "approved" ? "View Report" : "Inspect & Draft",
          onclick: () => openAlertModal(alert.alert_id)
        })
      )
    );
    tbody.append(tr);
  });
}

// ---------------------------------------------------------------------------
// 8. Dedicated Alert Detail Modal (5 Required Sections)
// ---------------------------------------------------------------------------

async function openAlertModal(alertId) {
  state.selectedAlertId = alertId;
  const modal = $("modal-alert-detail");
  if (!modal) return;

  $("alert-modal-title").textContent = `Prescribing Cascade Investigation: #${alertId}`;
  $("alert-modal-sub").textContent = "Loading clinical details...";
  $("det-sec-a-text").textContent = "Fetching patient sequence and known cascade data...";
  $("det-sec-b-timeline").replaceChildren();
  $("det-sec-c-reasons").replaceChildren();
  $("det-sec-d-evidence").textContent = "Retrieving population sequence evidence and FAERS cache...";
  $("modal-dismiss-box").hidden = true;
  modal.hidden = false;

  try {
    const detail = await api(`/alerts/${encodeURIComponent(alertId)}`, state.phKey);
    state.alerts.set(alertId, detail);

    $("alert-modal-sub").textContent = `Alert #${detail.alert_id} · Pharmacy ${detail.pharmacy_id} · Status: ${detail.status || "open"}`;

    // Section A: What happened?
    $("det-sec-a-text").textContent =
      `Patient was initiated on ${detail.index_drug} (${detail.index_dose_mg ? detail.index_dose_mg + 'mg' : 'standard dose'}), ` +
      `and subsequently prescribed ${detail.marker_drug} (${detail.marker_dose_mg ? detail.marker_dose_mg + 'mg' : 'standard dose'}) ` +
      `${detail.days_between} days later. ` +
      (detail.known_source
        ? `This sequence is an established prescribing cascade documented in ${detail.known_source}, where side-effects (e.g. ${detail.suspected_reaction || 'adverse reaction'}) are commonly mistaken for new clinical conditions.`
        : `This sequence was identified as an emerging prescribing cascade signal with population-level directional asymmetry.`) +
      ` Professional clinical evaluation is advised to determine whether ${detail.marker_drug} was prescribed to treat an unrecognised adverse reaction to ${detail.index_drug}.`;

    // Section B: Medication Timeline
    const timeline = $("det-sec-b-timeline");
    timeline.replaceChildren(
      el("div", { class: "timeline-step" },
        el("span", { class: "timeline-step-badge", text: "Day 0" }),
        el("strong", { style: "margin-top:4px;font-size:12px;", text: detail.index_drug }),
        el("span", { style: "font-size:11px;color:var(--text-muted);", text: "Initiated" })
      ),
      el("div", { class: "timeline-arrow", text: "➔" }),
      el("div", { class: "timeline-step" },
        el("span", { class: "timeline-step-badge", style: "border-color:#FCA5A5;background:#FEE2E2;color:#B42318;", text: `+${detail.days_between} Days` }),
        el("strong", { style: "margin-top:4px;font-size:12px;", text: detail.marker_drug }),
        el("span", { style: "font-size:11px;color:var(--text-muted);", text: "Started (Cascade)" })
      ),
      el("div", { class: "timeline-arrow", text: "➔" }),
      el("div", { class: "timeline-step" },
        el("span", { class: "timeline-step-badge", style: "border-color:#86EFAC;background:#DCFCE7;color:#16803D;", text: "Counter Interception" }),
        el("strong", { style: "margin-top:4px;font-size:12px;", text: "CASCADE Alert" }),
        el("span", { style: "font-size:11px;color:var(--text-muted);", text: `Score: ${fmt(detail.score, 2)}` })
      )
    );

    // Section C: Explainable AI TreeSHAP Factors
    const reasonsList = $("det-sec-c-reasons");
    reasonsList.replaceChildren();
    if (detail.reasons && detail.reasons.length) {
      detail.reasons.forEach((reason) => {
        reasonsList.append(el("li", { text: reason }));
      });
    } else {
      reasonsList.append(el("li", { text: "No feature explanation factors returned by scorer." }));
    }

    // Section D: Statistical & FAERS Evidence
    const evidenceBox = $("det-sec-d-evidence");
    const ev = detail.evidence || {};
    evidenceBox.innerHTML = `
      <strong>Adjusted Sequence Ratio (aSR):</strong> ${fmt(ev.aSR, 2)} ${ev.aSR_ci ? `[95% CI: ${fmt(ev.aSR_ci[0], 2)} – ${fmt(ev.aSR_ci[1], 2)}]` : ''}<br>
      <strong>Literature Source:</strong> ${ev.known_source || 'Discovered Population Signal'}<br>
      <strong>FAERS External Pharmacovigilance Context:</strong> ${ev.faers || 'No external FAERS cache data'}
    `;

    // Modal action button state
    $("btn-modal-create-draft").onclick = () => {
      modal.hidden = true;
      navigateTo("reports", null, true);
      triggerDraftForAlert(alertId);
    };

    $("btn-modal-dismiss").onclick = () => {
      $("modal-dismiss-box").hidden = false;
      $("modal-dismiss-input").focus();
    };

    $("btn-modal-confirm-dismiss").onclick = async () => {
      const reason = $("modal-dismiss-input").value.trim();
      if (!reason) {
        showToast("Please enter a dismissal reason for model feedback.", true);
        return;
      }
      try {
        await api(`/alerts/${encodeURIComponent(alertId)}/dismiss`, state.phKey, {
          method: "POST",
          body: JSON.stringify({ reason })
        });
        showToast(`Alert #${alertId} dismissed. Recorded feedback for model training.`);
        modal.hidden = true;
        loadAlerts();
      } catch (e) {
        showToast(`Dismissal failed: ${e.message}`, true);
      }
    };
  } catch (err) {
    showToast(`Error fetching alert details: ${err.message}`, true);
  }
}

// ---------------------------------------------------------------------------
// 9. Page 4: Population Signals (Loop 2 PSSA)
// ---------------------------------------------------------------------------

async function loadSignalsView() {
  if (state.role !== "regulator") {
    // If user is pharmacist, allow viewing with explanation
  }

  const w = $("signals-win-select")?.value || 90;
  const bh = $("signals-bh-checkbox")?.checked ? 1 : 0;

  try {
    const res = await api(`/signals?window=${w}&bh=${bh}&all=1`, state.regKey);
    state.signals = res.signals || [];

    const summary = $("signals-stat-summary");
    if (summary) {
      summary.textContent =
        `PSSA Discovery: ${res.n_signals || 0} candidate signals (${res.n_signals_bh || 0} meeting BH-FDR threshold) ` +
        `among ${res.tested_pairs || 0} tested drug pairs (Window: ${w} days)` +
        (res.sim_date ? ` · Sim Date: ${res.sim_date}` : "");
    }

    renderSignalsTable();
  } catch (err) {
    showToast(`Error loading population signals: ${err.message}`, true);
  }
}

function renderSignalsTable() {
  const tbody = $("signals-tbody");
  if (!tbody) return;
  tbody.replaceChildren();

  if (!state.signals.length) {
    tbody.append(
      el("tr", {},
        el("td", { colspan: "7", style: "text-align:center;padding:36px;color:var(--text-muted);" },
          el("div", { style: "font-size:14px;font-weight:600;", text: "No population signals detected for selected parameters." })
        )
      )
    );
    return;
  }

  state.signals.forEach((sig) => {
    const isNovacor = sig.a?.fictional || sig.b?.fictional || (sig.a?.atc_l4 === "C09XX" && sig.b?.atc_l4 === "A02BC");
    const tr = el("tr", { class: isNovacor ? "highlight-h1" : "" },
      el("td", {},
        el("strong", { text: `${sig.a?.atc_l4} (${sig.a?.examples?.[0] || 'Drug A'})` }),
        " ➔ ",
        el("span", { style: "color:var(--color-purple);font-weight:600;", text: `${sig.b?.atc_l4} (${sig.b?.examples?.[0] || 'Drug B'})` }),
        isNovacor ? el("span", { class: "badge badge-novel", style: "margin-left:6px;", text: "🚨 Novel Market Signal" }) : null
      ),
      el("td", { class: "font-mono font-bold", text: `${fmt(sig.asr, 2)} [${fmt(sig.lo, 2)} – ${fmt(sig.hi, 2)}]` }),
      el("td", { text: String(sig.n ?? "—") }),
      el("td", { class: "font-mono", text: `${sig.n_ab ?? '—'} : ${sig.n_ba ?? '—'}` }),
      el("td", { class: "small", text: sig.first_signal_date || "—" }),
      el("td", {},
        el("span", { class: `badge ${sig.status === 'new' ? 'badge-novel' : 'badge-known'}`, text: sig.status === 'new' ? 'Novel Signal' : sig.status })
      ),
      el("td", {},
        el("button", {
          type: "button",
          class: "btn btn-sm btn-secondary",
          text: "View Trajectory Chart",
          onclick: () => loadSignalHistory(sig.a?.atc_l4, sig.b?.atc_l4, `${sig.a?.atc_l4} ➔ ${sig.b?.atc_l4}`)
        })
      )
    );
    tbody.append(tr);
  });
}

async function loadSignalHistory(a, b, title) {
  const w = $("signals-win-select")?.value || 90;
  try {
    const hist = await api(`/signals/${encodeURIComponent(a)}/${encodeURIComponent(b)}/history?window=${w}`, state.regKey);
    const card = $("signals-chart-card");
    if (!card) return;
    card.hidden = false;
    $("signals-chart-title").textContent = `Longitudinal aSR Trajectory: ${title} (Window: ${w}d)`;

    const canvas = $("signals-history-chart");
    if (!canvas || !window.Chart) return;

    if (state.chartInstance) {
      state.chartInstance.destroy();
    }

    const clamp = (v) => (v == null ? null : Math.min(Math.max(v, 0.05), 150));
    const labels = hist.points.map((p) => p.sim_date || `Day ${p.sim_day}`);

    state.chartInstance = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Lower 95% Bound",
            data: hist.points.map((p) => clamp(p.lo)),
            borderWidth: 0,
            pointRadius: 0,
            fill: false
          },
          {
            label: "95% Clopper-Pearson Confidence Interval",
            data: hist.points.map((p) => clamp(p.hi)),
            borderWidth: 0,
            pointRadius: 0,
            backgroundColor: "rgba(21, 94, 239, 0.12)",
            fill: "-1"
          },
          {
            label: "Adjusted Sequence Ratio (aSR)",
            data: hist.points.map((p) => clamp(p.asr)),
            borderColor: "#155EEF",
            borderWidth: 2.2,
            pointRadius: 2,
            fill: false
          },
          {
            label: "Null Hypothesis Boundary (aSR = 1.0)",
            data: labels.map(() => 1.0),
            borderColor: "#94A3B8",
            borderDash: [5, 4],
            borderWidth: 1.5,
            pointRadius: 0,
            fill: false
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: {
            type: "logarithmic",
            title: { display: true, text: "aSR (Logarithmic scale)" }
          },
          x: {
            title: { display: true, text: "Simulated Time" },
            ticks: { maxTicksLimit: 8 }
          }
        },
        plugins: {
          legend: {
            labels: {
              filter: (item) => item.text !== "Lower 95% Bound"
            }
          }
        }
      }
    });

    card.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    showToast(`Error rendering history trajectory: ${err.message}`, true);
  }
}

// ---------------------------------------------------------------------------
// 10. Page 5: Reports & PvPI Workflow
// ---------------------------------------------------------------------------

async function loadReportsView() {
  if (state.role !== "pharmacist") {
    const tbody = $("reports-tbody");
    if (tbody) {
      tbody.replaceChildren(
        el("tr", {},
          el("td", { colspan: "7", style: "text-align:center;padding:36px;color:var(--text-secondary);" },
            el("div", { style: "font-weight:700;font-size:15px;margin-bottom:6px;color:var(--text-primary);", text: "Pharmacist Authentication Required" },
            el("p", { text: "Clinical reporting drafts and prescriber notes are available for authorized pharmacy personnel. Switch role to 'Pharmacist' to view drafts." })
          )
        )
      ));
    }
    return;
  }

  try {
    const list = await api("/reports", state.phKey);
    state.reports = list || [];
    renderReportsTable();
  } catch (err) {
    showToast(`Error loading reports: ${err.message}`, true);
  }
}

function renderReportsTable() {
  const tbody = $("reports-tbody");
  if (!tbody) return;
  tbody.replaceChildren();

  if (!state.reports.length) {
    tbody.append(
      el("tr", {},
        el("td", { colspan: "7", style: "text-align:center;padding:36px;color:var(--text-muted);" },
          el("div", { style: "font-size:14px;font-weight:600;margin-bottom:4px;", text: "No report drafts currently prepared" }),
          el("div", { style: "font-size:12px;", text: "Select an alert from 'Cascade Alerts' to create a peer note and PvPI report draft." })
        )
      )
    );
    return;
  }

  state.reports.forEach((rep) => {
    const draft = rep.draft || {};
    const tr = el("tr", {},
      el("td", { class: "font-mono font-bold", text: `REP-${rep.alert_id}` }),
      el("td", { text: rep.alert_id }),
      el("td", { text: `${rep.index_drug} ➔ ${rep.marker_drug}` }),
      el("td", {}, el("span", { class: `badge badge-${rep.status || 'drafted'}`, text: rep.status || 'drafted' })),
      el("td", {},
        el("span", {
          class: "badge",
          style: draft.generator?.startsWith("llm") ? "background:#F3E8FF;color:#7E22CE;" : "background:#E2E8F0;color:#334155;",
          text: draft.generator || "template"
        })
      ),
      el("td", {},
        draft.validator_pass
          ? el("span", { class: "badge badge-approved", text: "PASSED (Deterministic Verification)" })
          : el("span", { class: "badge badge-open", text: "Review Required" })
      ),
      el("td", {},
        el("button", {
          type: "button",
          class: "btn btn-sm btn-primary",
          text: "Preview & Export",
          onclick: () => triggerDraftForAlert(rep.alert_id)
        })
      )
    );
    tbody.append(tr);
  });
}

async function triggerDraftForAlert(alertId) {
  const container = $("reports-preview-container");
  if (!container) return;
  container.hidden = false;

  $("report-preview-title").textContent = `Clinical Intervention: Alert #${alertId}`;
  $("report-note-preview").textContent = "Generating AI-assisted clinical prescriber note...";
  $("report-form-preview").replaceChildren();

  try {
    if (!state.fields) {
      state.fields = await api("/pvpi/fields", currentKey());
    }

    const draft = await api(`/alerts/${encodeURIComponent(alertId)}/draft`, state.phKey, { method: "POST" });
    const detail = await api(`/alerts/${encodeURIComponent(alertId)}`, state.phKey);

    // Provenance bar
    const prov = $("report-provenance-bar");
    if (prov) {
      prov.innerHTML = `
        <span><strong>Document Engine:</strong> ${draft.generator.startsWith("llm") ? "LLM (" + draft.generator.slice(4) + ")" : "Deterministic Template Fallback"}</span>
        <span>·</span>
        <span><strong>Safety Validator:</strong> <span class="badge ${draft.validator_pass ? 'badge-approved' : 'badge-open'}">${draft.validator_pass ? "Passed (100% Deterministic Safety Rules)" : "Fallback Applied"}</span></span>
        <span>·</span>
        <span><strong>Generation Latency:</strong> ${draft.gen_ms} ms</span>
        <span>·</span>
        <span><strong>External Evidence:</strong> ${detail.evidence?.faers || 'No external cache note'}</span>
      `;
    }

    // Prescriber Note
    $("report-note-preview").textContent = draft.note_text;

    // PvPI Form Fields
    renderPvpiFields(draft.report);

    // Wire up approve & export button
    $("btn-report-approve").onclick = () => approveAndExport(alertId);

    container.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    showToast(`Error creating draft: ${err.message}`, true);
  }
}

function renderPvpiFields(report) {
  const box = $("report-form-preview");
  if (!box || !state.fields) return;
  box.replaceChildren();

  const tbc = "To be confirmed by reporting clinician";

  for (const sec of state.fields.sections || []) {
    const val = report[sec.key];
    const items = Array.isArray(val) ? (val.length ? val : [{}]) : [val || {}];

    items.forEach((item, idx) => {
      box.append(el("div", { class: "pvpi-section-heading", text: sec.title + (items.length > 1 ? ` (${idx + 1})` : "") }));
      const table = el("table", { class: "pvpi-table" });
      const tbody = el("tbody");

      for (const field of sec.fields || []) {
        const v = item ? item[field.key] : null;
        const empty = v == null || v === "";
        const row = el("tr", {},
          el("th", { scope: "row", text: field.label }),
          el("td", { class: empty ? "amber-verify" : "", text: empty ? tbc : String(v) })
        );
        tbody.append(row);
      }
      table.append(tbody);
      box.append(table);
    });
  }
}

async function approveAndExport(alertId) {
  const btn = $("btn-report-approve");
  if (btn) btn.disabled = true;

  try {
    const res = await api(`/alerts/${encodeURIComponent(alertId)}/approve`, state.phKey, { method: "POST" });

    // Download PDF
    const pdfBytes = Uint8Array.from(atob(res.file_base64), (c) => c.charCodeAt(0));
    downloadFile(res.file_name, res.file_type, new Blob([pdfBytes], { type: res.file_type }));

    // Download JSON Audit Copy
    downloadFile(
      `${alertId}_pvpi_audit.json`,
      "application/json",
      JSON.stringify({
        alert_id: alertId,
        approved_ts: res.approved_ts,
        status: "approved",
        note: res.note,
        report: res.report,
        generator: res.generator,
        validator_pass: res.validator_pass
      }, null, 2)
    );

    showToast(`✓ Alert #${alertId} approved! Official PDF report and JSON audit record exported.`);
    loadReportsView();
  } catch (err) {
    showToast(`Approval failed: ${err.message}`, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function downloadFile(name, type, data) {
  const blob = data instanceof Blob ? data : new Blob([data], { type });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}

// ---------------------------------------------------------------------------
// 11. Page 6: Audit Trail
// ---------------------------------------------------------------------------

async function loadAuditView() {
  try {
    const logs = await api("/audit?limit=100", currentKey());
    state.auditLogs = logs || [];
    renderAuditTable();
  } catch (err) {
    showToast(`Error loading audit logs: ${err.message}`, true);
  }
}

function renderAuditTable() {
  const tbody = $("audit-tbody");
  if (!tbody) return;
  tbody.replaceChildren();

  if (!state.auditLogs.length) {
    tbody.append(
      el("tr", {},
        el("td", { colspan: "5", style: "text-align:center;padding:36px;color:var(--text-muted);" },
          el("div", { style: "font-size:14px;font-weight:600;", text: "No audit records found." })
        )
      )
    );
    return;
  }

  state.auditLogs.forEach((log) => {
    const actionClass =
      log.action === "approve" ? "act-approve" :
      log.action === "draft" ? "act-draft" :
      log.action === "export" ? "act-export" :
      log.action === "dismiss" ? "act-dismiss" :
      log.action === "demo_start" ? "act-demo" : "act-view";

    const tr = el("tr", {},
      el("td", { class: "font-mono small", text: String(log.id) }),
      el("td", { class: "small", text: (log.ts || "").replace("T", " ").slice(0, 23) }),
      el("td", { text: log.role || "—" }),
      el("td", {}, el("span", { class: `audit-action-chip ${actionClass}`, text: log.action || "view" })),
      el("td", { class: "font-mono font-bold", text: log.object_id || "System" })
    );
    tbody.append(tr);
  });
}

// ---------------------------------------------------------------------------
// 12. Page 7: System Status
// ---------------------------------------------------------------------------

async function loadSystemView() {
  refreshMetrics();
  try {
    const health = await api("/health", null);
    if ($("sys-sse-badge")) $("sys-sse-badge").textContent = "● Online (200 OK)";
    showToast("System health checks passed: All services nominal.");
  } catch (err) {
    showToast(`Health check error: ${err.message}`, true);
  }
}

// ---------------------------------------------------------------------------
// 13. Role Management & Setup
// ---------------------------------------------------------------------------

function setRole(newRole) {
  state.role = newRole;

  const btnPh = $("role-btn-ph");
  const btnReg = $("role-btn-reg");
  const btnDoc = $("role-btn-doc");
  const btnPat = $("role-btn-pat");
  const avatar = $("role-avatar-icon");
  const title = $("role-display-title");
  const sub = $("role-display-sub");

  [btnPh, btnReg, btnDoc, btnPat].forEach((b) => b?.classList.remove("active"));

  if (newRole === "pharmacist") {
    btnPh?.classList.add("active");
    if (avatar) avatar.className = "role-avatar avatar-pharmacist";
    if (avatar) avatar.textContent = "PH";
    if (title) title.textContent = "Pharmacist PH030";
    if (sub) sub.textContent = "Loop 1 Counter Access";
    connectStream("ph", state.phKey);
    showToast("Switched active view to Pharmacist (Counter & Alerts)");
    navigateTo(state.activeRoute === "doctor" || state.activeRoute === "patient" ? "overview" : state.activeRoute, null, true);
  } else if (newRole === "regulator") {
    btnReg?.classList.add("active");
    if (avatar) avatar.className = "role-avatar avatar-regulator";
    if (avatar) avatar.textContent = "REG";
    if (title) title.textContent = "National Health Authority";
    if (sub) sub.textContent = "Loop 2 PSSA Signals Access";
    connectStream("reg", state.regKey);
    showToast("Switched active view to Regulator (Population Signals)");
    navigateTo(state.activeRoute === "doctor" || state.activeRoute === "patient" ? "signals" : state.activeRoute, null, true);
  } else if (newRole === "doctor") {
    btnDoc?.classList.add("active");
    if (avatar) avatar.className = "role-avatar avatar-doctor";
    if (avatar) avatar.textContent = "DOC";
    if (title) title.textContent = "Doctor Consultation Station";
    if (sub) sub.textContent = "EHR & Prescribing Pad";
    showToast("Switched active view to Doctor Station (EHR)");
    navigateTo("doctor", null, true);
  } else if (newRole === "patient") {
    btnPat?.classList.add("active");
    if (avatar) avatar.className = "role-avatar avatar-patient";
    if (avatar) avatar.textContent = "PAT";
    if (title) title.textContent = "MyHealth Patient Portal";
    if (sub) sub.textContent = "Longitudinal Care Journey";
    showToast("Switched active view to Patient Health Portal");
    navigateTo("patient", null, true);
  }
}

function connectStream(streamName, key) {
  openEventStream(streamName, key, (ev, data) => {
    if (ev === "event") {
      recordRecentEvent({
        isAlert: false,
        type: "Dispense",
        event_id: data.event_id,
        drug: data.drug,
        ts: data.ts,
        pharmacy_id: data.pharmacy_id,
        atc_l4: data.atc_l4
      });
    } else if (ev === "alert") {
      state.alerts.set(data.alert_id, { ...data, status: "open" });
      recordRecentEvent({
        isAlert: true,
        type: "Cascade Alert",
        alertId: data.alert_id,
        drug: `${data.index_drug} ➔ ${data.marker_drug}`,
        ts: data.ts,
        pharmacy_id: data.pharmacy_id,
        score: data.score
      });
      renderAlertsTable();
      updateActionQueue();
      showToast(`🚨 Prescribing Cascade Alert: ${data.index_drug} ➔ ${data.marker_drug} (Score: ${fmt(data.score, 2)})`, true);
    } else if (ev === "signals") {
      showToast("🌐 Population signals recomputed by Loop 2 PSSA engine.", false);
      if (state.activeRoute === "signals") {
        loadSignalsView();
      }
    }
  });
}

// ---------------------------------------------------------------------------
// 14. Clinical EHR (Doctor Station) & Patient Portal
// ---------------------------------------------------------------------------

async function loadDoctorStation() {
  const tsInput = $("doc-visit-ts");
  if (tsInput) {
    const now = new Date();
    tsInput.value = now.toISOString().slice(0, 16).replace("T", " ");
  }

  try {
    // 1. Load doctors if not loaded
    if (state.doctors.length === 0) {
      const res = await fetch("/api/clinical/doctors");
      if (res.ok) {
        state.doctors = await res.json();
        const sel = $("doc-doctor-select");
        if (sel) {
          sel.innerHTML = "";
          state.doctors.forEach((d) => {
            const opt = el("option", { value: d.doctor_id }, `${d.name} (${d.specialty})`);
            sel.appendChild(opt);
          });
          const priya = state.doctors.find((d) => d.doctor_id === "DR0020");
          if (priya) {
            sel.value = "DR0020";
            state.selectedDoctorId = "DR0020";
          } else if (state.doctors[0]) {
            state.selectedDoctorId = state.doctors[0].doctor_id;
          }
        }
      }
    }

    // 2. Load patients if not loaded
    if (state.patients.length === 0) {
      const res = await fetch("/api/clinical/patients");
      if (res.ok) {
        state.patients = await res.json();
        const sel = $("doc-patient-select");
        if (sel) {
          sel.innerHTML = "";
          state.patients.forEach((p) => {
            const opt = el("option", { value: p.patient_id }, `${p.name} [${p.patient_id}] (${p.age_band} · ${p.sex})`);
            sel.appendChild(opt);
          });
          if (state.selectedPatientId) {
            sel.value = state.selectedPatientId;
          }
        }
      }
    }

    // 3. Load catalogue drugs
    if (state.availableDrugs.length === 0) {
      const res = await fetch("/api/clinical/drugs");
      if (res.ok) {
        state.availableDrugs = await res.json();
        const sel = $("doc-rx-drug-select");
        if (sel) {
          sel.innerHTML = '<option value="">-- Choose drug from catalogue --</option>';
          state.availableDrugs.forEach((d) => {
            const opt = el("option", { value: d.drug_id, "data-name": d.generic_name, "data-dose": d.dose_mg, "data-l4": d.atc_l4 }, `${d.display_name} (${d.dose_mg}mg) [${d.atc_l4}]`);
            sel.appendChild(opt);
          });
        }
      }
    }

    // 4. Load reactions autocomplete
    if (state.reactions.length === 0) {
      const res = await fetch("/api/clinical/reactions");
      if (res.ok) {
        state.reactions = await res.json();
        const dl = $("reactions-datalist");
        if (dl) {
          dl.innerHTML = "";
          state.reactions.forEach((r) => {
            dl.appendChild(el("option", { value: r }));
          });
        }
      }
    }

    // Load active patient details
    await loadPatientDetails(state.selectedPatientId || "P001142");
  } catch (err) {
    showToast(`Error initializing Doctor Station: ${err.message}`, true);
  }
}

async function loadPatientDetails(patientId) {
  state.selectedPatientId = patientId;
  const docSel = $("doc-patient-select");
  if (docSel && docSel.value !== patientId) docSel.value = patientId;

  try {
    const res = await fetch(`/api/clinical/patient/${patientId}/timeline`);
    if (!res.ok) throw new Error("Failed to load patient timeline");
    const data = await res.json();
    state.clinicalTimeline = data;

    // 1. Render Demographics Banner
    const p = data.patient;
    const demoStrip = $("doc-patient-demographics");
    if (demoStrip) {
      demoStrip.innerHTML = "";
      demoStrip.appendChild(el("span", { class: "demographic-pill" }, el("span", { text: "ID: " }), el("strong", { text: p.patient_id })));
      demoStrip.appendChild(el("span", { class: "demographic-pill" }, el("span", { text: "Age: " }), el("strong", { text: `${p.age_band} yrs (${p.sex})` })));
      demoStrip.appendChild(el("span", { class: "demographic-pill" }, el("span", { text: "Blood: " }), el("strong", { text: p.blood_group })));
      if (p.phone) demoStrip.appendChild(el("span", { class: "demographic-pill" }, el("span", { text: "Phone: " }), el("strong", { text: p.phone })));
      if (p.emergency_contact) demoStrip.appendChild(el("span", { class: "demographic-pill" }, el("span", { text: "Emergency: " }), el("strong", { text: p.emergency_contact })));
    }

    const condsBox = $("doc-patient-conditions");
    if (condsBox) {
      condsBox.innerHTML = "";
      (p.chronic_conditions || []).forEach((c) => {
        condsBox.appendChild(el("span", { class: "condition-tag", text: c }));
      });
    }

    // 2. Render Longitudinal Encounters
    const encFeed = $("doc-encounter-feed");
    const encCount = $("doc-encounter-count");
    if (encCount) encCount.textContent = `${data.encounters.length} Visits`;
    if (encFeed) {
      encFeed.innerHTML = "";
      if (data.encounters.length === 0) {
        encFeed.appendChild(el("p", { style: "font-size:12px;color:var(--text-secondary);padding:10px;", text: "No previous clinical encounters recorded." }));
      } else {
        data.encounters.forEach((enc) => {
          const card = el("div", { class: `encounter-card ${enc.cascade_flag ? "cascade-flagged" : ""}` });
          
          const dStr = enc.visit_ts ? enc.visit_ts.slice(0, 16).replace("T", " ") : "";
          const metaRow = el("div", { class: "enc-meta-row" },
            el("span", { class: "enc-date", text: dStr }),
            enc.cascade_flag ? el("span", { class: "badge badge-critical", text: "Cascade Intercepted" }) : el("span", { class: "badge badge-neutral", text: "Routine" })
          );
          card.appendChild(metaRow);

          const docRow = el("div", { class: "enc-doctor-row" },
            el("span", { class: "enc-doctor-name", text: enc.doctor_name }),
            el("span", { class: "enc-doctor-spec", text: `· ${enc.specialty} (${enc.hospital})` })
          );
          card.appendChild(docRow);

          const causeBox = el("div", { class: "enc-cause-box" },
            el("span", { class: "enc-cause-label", text: "Chief Complaint / Reason for Visit:" }),
            el("span", { text: enc.chief_complaint })
          );
          card.appendChild(causeBox);

          const diag = el("div", { class: "enc-diagnosis", text: `Diagnosis: ${enc.diagnosis}` });
          card.appendChild(diag);

          if (enc.clinical_notes) {
            const notes = el("p", { style: "font-size:11.5px;color:var(--text-secondary);margin:0 0 6px;line-height:1.35;", text: enc.clinical_notes });
            card.appendChild(notes);
          }

          if (enc.prescriptions && enc.prescriptions.length > 0) {
            const chips = el("div", { class: "enc-rx-chips" });
            enc.prescriptions.forEach((rx) => {
              const isCasc = enc.cascade_flag && (rx.generic_name.toLowerCase().includes("furosemide") || rx.generic_name.toLowerCase().includes("dextromethorphan"));
              chips.appendChild(el("span", { class: `rx-chip ${isCasc ? "cascade-drug" : ""}`, text: `Rx: ${rx.generic_name} ${rx.dose_mg}mg (${rx.frequency})` }));
            });
            card.appendChild(chips);
          }

          encFeed.appendChild(card);
        });
      }
    }

    // 3. Render Active Regimen (Column 3)
    const actFeed = $("doc-active-drugs-feed");
    const actCount = $("doc-active-rx-count");
    if (actCount) actCount.textContent = `${data.active_prescriptions.length} Active`;
    if (actFeed) {
      actFeed.innerHTML = "";
      if (data.active_prescriptions.length === 0) {
        actFeed.appendChild(el("p", { style: "font-size:12px;color:var(--text-secondary);", text: "No active medications on file." }));
      } else {
        data.active_prescriptions.forEach((rx) => {
          const card = el("div", { class: "active-drug-card" },
            el("div", { class: "active-drug-top" },
              el("span", { text: `${rx.generic_name.toUpperCase()} ${rx.dose_mg}mg` }),
              el("span", { class: "badge badge-purple", text: rx.atc_l4 })
            ),
            el("div", { class: "active-drug-meta", text: `${rx.frequency} · ${rx.duration_days} days supply` }),
            el("div", { style: "font-size:11px;color:var(--text-secondary);margin-bottom:2px;", text: `For: ${rx.indication || 'Maintenance'}` }),
            el("div", { class: "active-drug-prescriber", text: `Prescribed by: ${rx.doctor_name} (${rx.specialty})` })
          );
          if (rx.warning_note) {
            card.appendChild(el("div", { style: "font-size:10.5px;color:#B91C1C;margin-top:4px;font-weight:600;", text: `⚠️ ${rx.warning_note}` }));
          }
          actFeed.appendChild(card);
        });
      }
    }

    // 4. Render Patient Reported Symptoms
    const symFeed = $("doc-symptoms-feed");
    if (symFeed) {
      symFeed.innerHTML = "";
      if (data.symptoms.length === 0) {
        symFeed.appendChild(el("p", { style: "font-size:11px;color:var(--text-secondary);margin:0;", text: "No adverse symptoms reported by patient." }));
      } else {
        data.symptoms.forEach((s) => {
          const sCard = el("div", { class: "symptom-card" },
            el("div", { class: "symptom-title", text: `Symptom: ${s.symptom}` }),
            el("div", { style: "font-size:10.5px;color:#78350F;", text: `Severity: ${s.severity.toUpperCase()} · Reported: ${s.created_ts ? s.created_ts.slice(0, 10) : ''}` }),
            s.suspected_drug ? el("div", { style: "font-size:10.5px;color:#B45309;font-weight:600;", text: `Suspected: ${s.suspected_drug}` }) : null,
            s.notes ? el("div", { style: "font-size:10.5px;color:#92400E;margin-top:2px;", text: `Notes: "${s.notes}"` }) : null
          );
          symFeed.appendChild(sCard);
        });
      }
    }

    // Clear current Rx pad
    state.currentPrescriptions = [];
    renderPrescriptionPadItems();
    if ($("doc-cascade-copilot")) $("doc-cascade-copilot").hidden = true;
  } catch (err) {
    showToast(`Error fetching patient timeline: ${err.message}`, true);
  }
}

// Live CASCADE Pre-Check
function debouncePrecheck() {
  clearTimeout(state.precheckDebounceTimer);
  state.precheckDebounceTimer = setTimeout(() => {
    checkCascadeLive();
  }, 250);
}

function handleDrugSelectionChange() {
  const sel = $("doc-rx-drug-select");
  if (!sel || !sel.value) return;
  const opt = sel.selectedOptions[0];
  if (opt) {
    const dose = opt.getAttribute("data-dose");
    if (dose && $("doc-rx-dose")) $("doc-rx-dose").value = dose;
  }
  checkCascadeLive();
}

async function checkCascadeLive() {
  const drugSel = $("doc-rx-drug-select");
  const copilot = $("doc-cascade-copilot");
  if (!drugSel || !drugSel.value || !copilot) {
    if (copilot) copilot.hidden = true;
    return;
  }

  const opt = drugSel.selectedOptions[0];
  const genericName = opt ? opt.getAttribute("data-name") : "";
  const complaint = $("doc-chief-complaint") ? $("doc-chief-complaint").value.trim() : "";
  const indication = $("doc-rx-indication") ? $("doc-rx-indication").value.trim() : "";

  try {
    const res = await fetch("/api/clinical/precheck-cascade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        patient_id: state.selectedPatientId,
        drug_id: drugSel.value,
        generic_name: genericName,
        indication: indication,
        chief_complaint: complaint
      })
    });
    if (!res.ok) return;
    const data = await res.json();

    if (data.is_cascade) {
      copilot.hidden = false;
      const lvl = $("doc-copilot-level");
      if (lvl) lvl.textContent = data.risk_level === "HIGH" ? "HIGH SUSPICION CASCADE" : "POSSIBLE CASCADE RISK";
      const title = $("doc-copilot-title");
      if (title) title.textContent = `Prescribing Cascade Warning: ${data.reaction}`;
      const text = $("doc-copilot-text");
      if (text) text.textContent = `Patient is currently on ${data.prior_drug} started on ${data.prior_date} by ${data.prior_doctor} (${data.prior_specialty}). The symptom '${data.reaction}' is a documented adverse reaction to that medication.`;
      const ev = $("doc-copilot-evidence");
      if (ev) ev.innerHTML = `<strong>CASCADE Statistical Score:</strong> ${(data.score * 100).toFixed(0)}% confidence · <strong>Attending:</strong> ${data.prior_doctor}`;
      const rec = $("doc-copilot-recom");
      if (rec) rec.innerHTML = `<strong>Clinical Recommendation:</strong> ${data.recommendation}`;
    } else {
      copilot.hidden = true;
    }
  } catch (err) {
    console.error("Live cascade check error:", err);
  }
}

function addPrescriptionItem() {
  const sel = $("doc-rx-drug-select");
  if (!sel || !sel.value) {
    showToast("Please choose a medication from the catalogue first.", true);
    return;
  }
  const opt = sel.selectedOptions[0];
  const drugId = sel.value;
  const genericName = opt.getAttribute("data-name") || "";
  const atcL4 = opt.getAttribute("data-l4") || "";
  const dose = parseFloat($("doc-rx-dose").value) || 0;
  const freq = $("doc-rx-frequency").value;
  const duration = parseInt($("doc-rx-duration").value, 10) || 30;
  const indication = $("doc-rx-indication").value.trim() || $("doc-chief-complaint").value.trim() || "Clinical Indication";

  const isCascade = $("doc-cascade-copilot") && !$("doc-cascade-copilot").hidden;

  state.currentPrescriptions.push({
    drug_id: drugId,
    generic_name: genericName,
    atc_l4: atcL4,
    dose_mg: dose,
    frequency: freq,
    duration_days: duration,
    indication: indication,
    is_cascade: isCascade
  });

  sel.value = "";
  $("doc-rx-dose").value = "";
  $("doc-rx-indication").value = "";
  if ($("doc-cascade-copilot")) $("doc-cascade-copilot").hidden = true;

  renderPrescriptionPadItems();
  showToast(`Added ${genericName} (${dose}mg) to prescription slip.`);
}

function removePrescriptionItem(idx) {
  state.currentPrescriptions.splice(idx, 1);
  renderPrescriptionPadItems();
}

function renderPrescriptionPadItems() {
  const tbody = $("doc-rx-items-body");
  if (!tbody) return;
  tbody.innerHTML = "";

  if (state.currentPrescriptions.length === 0) {
    const tr = el("tr", {}, el("td", { colspan: "7", style: "text-align:center;color:var(--text-secondary);padding:12px;", text: "No medications added to this prescription slip yet. Select a drug above." }));
    tbody.appendChild(tr);
    return;
  }

  state.currentPrescriptions.forEach((rx, idx) => {
    const tr = el("tr", {},
      el("td", {}, el("strong", { text: rx.generic_name.toUpperCase() }), el("span", { style: "display:block;font-size:10.5px;color:var(--text-secondary);font-family:var(--font-mono);", text: rx.atc_l4 })),
      el("td", { text: `${rx.dose_mg} mg` }),
      el("td", { text: rx.frequency }),
      el("td", { text: `${rx.duration_days} d` }),
      el("td", { text: rx.indication }),
      el("td", {}, rx.is_cascade ? el("span", { class: "badge badge-critical", text: "⚠️ Prescribing Cascade" }) : el("span", { class: "badge badge-green", text: "Standard" })),
      el("td", {}, el("button", { type: "button", class: "btn-remove-rx", title: "Remove drug", onclick: () => removePrescriptionItem(idx) }, "✕"))
    );
    tbody.appendChild(tr);
  });
}

async function commitDoctorEncounter() {
  const patientId = state.selectedPatientId;
  const docSel = $("doc-doctor-select");
  const doctorId = docSel ? docSel.value : (state.selectedDoctorId || "DR0020");
  const chief = $("doc-chief-complaint") ? $("doc-chief-complaint").value.trim() : "";
  const diag = $("doc-diagnosis") ? $("doc-diagnosis").value.trim() : "";
  const notes = $("doc-clinical-notes") ? $("doc-clinical-notes").value.trim() : "";

  if (!chief) {
    showToast("Please enter the Chief Complaint / Cause of visit.", true);
    $("doc-chief-complaint")?.focus();
    return;
  }
  if (!diag) {
    showToast("Please specify a provisional or final diagnosis.", true);
    $("doc-diagnosis")?.focus();
    return;
  }

  const vitals = {
    bp: $("doc-vital-bp")?.value || "120/80",
    pulse: $("doc-vital-pulse")?.value || "72",
    spo2: $("doc-vital-spo2")?.value || "98%",
    weight: $("doc-vital-weight")?.value ? `${$("doc-vital-weight").value} kg` : "65 kg",
    temp: $("doc-vital-temp")?.value ? `${$("doc-vital-temp").value} F` : "98.4 F"
  };

  const payload = {
    patient_id: patientId,
    doctor_id: doctorId,
    visit_ts: new Date().toISOString(),
    chief_complaint: chief,
    vitals: vitals,
    clinical_notes: notes,
    diagnosis: diag,
    prescriptions: state.currentPrescriptions
  };

  try {
    const res = await fetch("/api/clinical/encounter/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) throw new Error("Server rejected clinical encounter");
    const result = await res.json();

    if (result.cascade_flag) {
      showToast("🚨 Encounter recorded: Prescribing Cascade Intercepted & Pharmacovigilance Alert Created!", true);
    } else {
      showToast("✅ Encounter and Prescription committed to patient record.");
    }

    resetDoctorConsultationForm();
    await loadPatientDetails(patientId);
  } catch (err) {
    showToast(`Failed to record encounter: ${err.message}`, true);
  }
}

function resetDoctorConsultationForm() {
  if ($("doc-chief-complaint")) $("doc-chief-complaint").value = "";
  if ($("doc-diagnosis")) $("doc-diagnosis").value = "";
  if ($("doc-clinical-notes")) $("doc-clinical-notes").value = "";
  if ($("doc-vital-bp")) $("doc-vital-bp").value = "";
  if ($("doc-vital-pulse")) $("doc-vital-pulse").value = "";
  if ($("doc-vital-spo2")) $("doc-vital-spo2").value = "";
  if ($("doc-vital-weight")) $("doc-vital-weight").value = "";
  if ($("doc-vital-temp")) $("doc-vital-temp").value = "";
  if ($("doc-rx-drug-select")) $("doc-rx-drug-select").value = "";
  if ($("doc-rx-dose")) $("doc-rx-dose").value = "";
  if ($("doc-rx-indication")) $("doc-rx-indication").value = "";
  if ($("doc-cascade-copilot")) $("doc-cascade-copilot").hidden = true;
  state.currentPrescriptions = [];
  renderPrescriptionPadItems();
}

// ---------------------------------------------------------------------------
// 15. Patient Portal (MyHealth)
// ---------------------------------------------------------------------------

async function loadPatientPortal() {
  try {
    if (state.patients.length === 0) {
      const res = await fetch("/api/clinical/patients");
      if (res.ok) {
        state.patients = await res.json();
      }
    }
    const sel = $("pat-patient-select");
    if (sel && state.patients.length > 0) {
      sel.innerHTML = "";
      state.patients.forEach((p) => {
        const opt = el("option", { value: p.patient_id }, `${p.name} (${p.age_band} · ${p.blood_group})`);
        sel.appendChild(opt);
      });
      if (state.selectedPatientId) sel.value = state.selectedPatientId;
    }
    await renderPatientPortal(state.selectedPatientId || "P001142");
  } catch (err) {
    showToast(`Error initializing Patient Portal: ${err.message}`, true);
  }
}

async function renderPatientPortal(patientId) {
  state.selectedPatientId = patientId;
  const patSel = $("pat-patient-select");
  if (patSel && patSel.value !== patientId) patSel.value = patientId;

  try {
    const res = await fetch(`/api/clinical/patient/${patientId}/timeline`);
    if (!res.ok) throw new Error("Failed to load patient record");
    const data = await res.json();
    const p = data.patient;

    // 1. Profile strip
    const prof = $("pat-profile-card");
    if (prof) {
      prof.innerHTML = "";
      const initials = p.name ? p.name.split(" ").map(w => w[0]).join("").slice(0, 2) : "PT";
      prof.appendChild(el("div", { class: "patient-avatar-lg", text: initials }));
      const infoBox = el("div", { style: "flex:1;" },
        el("h3", { style: "margin:0 0 4px;font-size:16px;color:var(--text-primary);", text: p.name }),
        el("div", { style: "display:flex;gap:12px;font-size:12px;color:var(--text-secondary);" },
          el("span", { text: `Patient ID: ${p.patient_id}` }),
          el("span", { text: `Age Group: ${p.age_band}` }),
          el("span", { text: `Gender: ${p.sex}` }),
          el("span", { text: `Blood Group: ${p.blood_group}` }),
          p.phone ? el("span", { text: `Phone: ${p.phone}` }) : null
        )
      );
      prof.appendChild(infoBox);
    }

    // 2. Safety Banner
    const safetyBanner = $("pat-safety-banner");
    if (safetyBanner) {
      if (data.active_cascade_warnings && data.active_cascade_warnings.length > 0) {
        safetyBanner.hidden = false;
        const sTitle = $("pat-safety-title");
        if (sTitle) sTitle.textContent = "Medication Review Advisory: Potential Drug Cascade Identified";
        const sText = $("pat-safety-text");
        if (sText) {
          sText.innerHTML = `Our medication-safety surveillance flagged that your recently prescribed medicine 
          (such as <strong>Furosemide</strong> for ankle swelling) is likely treating a common side effect of your blood pressure 
          tablet (<strong>Amlodipine</strong>). A notification has been recorded for your doctor's review.`;
        }
      } else {
        safetyBanner.hidden = true;
      }
    }

    // 3. Chronological Doctor Visits
    const visitsFeed = $("pat-visits-feed");
    if (visitsFeed) {
      visitsFeed.innerHTML = "";
      if (data.encounters.length === 0) {
        visitsFeed.appendChild(el("p", { style: "font-size:13px;color:var(--text-secondary);", text: "You have no past doctor visits recorded." }));
      } else {
        data.encounters.forEach((enc) => {
          const card = el("div", { class: `patient-visit-card ${enc.cascade_flag ? "cascade-flagged" : ""}` });

          const dStr = enc.visit_ts ? enc.visit_ts.slice(0, 16).replace("T", " at ") : "Recent";
          const header = el("div", { class: "pat-visit-header" },
            el("div", { class: "pat-doctor-info" },
              el("span", { class: "pat-doc-name", text: `Consultation with ${enc.doctor_name}` }),
              el("span", { class: "pat-doc-clinic", text: `${enc.specialty} · ${enc.hospital}` })
            ),
            el("span", { class: "pat-visit-date-badge", text: dStr })
          );
          card.appendChild(header);

          const cause = el("div", { class: "pat-cause-callout" },
            el("span", { class: "pat-cause-label", text: "Why You Visited (Medical Cause):" }),
            el("p", { class: "pat-cause-text", text: `"${enc.chief_complaint}"` })
          );
          card.appendChild(cause);

          const diag = el("div", { class: "pat-diagnosis-line" },
            el("strong", { text: "Doctor's Assessment / Diagnosis: " }),
            el("span", { text: enc.diagnosis })
          );
          card.appendChild(diag);

          if (enc.prescriptions && enc.prescriptions.length > 0) {
            const rxWrap = el("div", { style: "margin-top:10px;" },
              el("div", { style: "font-size:12px;font-weight:700;color:var(--text-primary);margin-bottom:6px;", text: "Medicines Prescribed During This Visit:" })
            );
            const grid = el("div", { class: "pat-rx-grid" });
            enc.prescriptions.forEach((rx) => {
              const rxCard = el("div", { class: "pat-rx-card" },
                el("div", { class: "pat-rx-top" },
                  el("span", { text: `💊 ${rx.generic_name.toUpperCase()} (${rx.dose_mg}mg)` }),
                  rx.warning_note ? el("span", { class: "badge badge-critical", text: "Safety Flag" }) : el("span", { class: "badge badge-green", text: "Prescribed" })
                ),
                el("div", { class: "pat-rx-schedule", text: `Instructions: Take ${rx.frequency} for ${rx.duration_days} days` }),
                el("div", { class: "pat-rx-reason", text: `Prescribed to treat: ${rx.indication || enc.chief_complaint}` })
              );
              grid.appendChild(rxCard);
            });
            rxWrap.appendChild(grid);
            card.appendChild(rxWrap);
          }

          visitsFeed.appendChild(card);
        });
      }
    }

    // 4. Active Cabinet
    const cabFeed = $("pat-active-meds-feed");
    const drugSel = $("pat-sym-drug");
    if (drugSel) drugSel.innerHTML = '<option value="">-- I am not sure --</option>';
    if (cabFeed) {
      cabFeed.innerHTML = "";
      if (data.active_prescriptions.length === 0) {
        cabFeed.appendChild(el("p", { style: "font-size:12px;color:var(--text-secondary);", text: "You have no active prescriptions." }));
      } else {
        data.active_prescriptions.forEach((rx) => {
          const item = el("div", { class: "cabinet-item" },
            el("div", { class: "cabinet-item-top" },
              el("span", { text: `💊 ${rx.generic_name.toUpperCase()} ${rx.dose_mg}mg` }),
              el("span", { class: "badge badge-blue", text: "Active" })
            ),
            el("div", { class: "cabinet-item-doc", text: `Prescribed by ${rx.doctor_name} (${rx.specialty})` }),
            el("div", { class: "cabinet-item-cause", text: `Purpose: ${rx.indication || 'Maintenance therapy'}` }),
            el("div", { style: "font-size:11px;color:var(--text-secondary);margin-top:2px;", text: `Schedule: ${rx.frequency}` })
          );
          cabFeed.appendChild(item);

          if (drugSel) {
            drugSel.appendChild(el("option", { value: rx.generic_name }, `${rx.generic_name} (${rx.dose_mg}mg)`));
          }
        });
      }
    }
  } catch (err) {
    showToast(`Error rendering Patient Portal: ${err.message}`, true);
  }
}

async function submitPatientSymptom() {
  const symName = $("pat-sym-name") ? $("pat-sym-name").value.trim() : "";
  if (!symName) {
    showToast("Please enter the symptom you are experiencing.", true);
    $("pat-sym-name")?.focus();
    return;
  }
  const sev = $("pat-sym-severity") ? $("pat-sym-severity").value : "moderate";
  const susp = $("pat-sym-drug") ? $("pat-sym-drug").value : "";
  const notes = $("pat-sym-notes") ? $("pat-sym-notes").value.trim() : "";

  try {
    const res = await fetch("/api/clinical/patient/report-symptom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        patient_id: state.selectedPatientId,
        symptom: symName,
        severity: sev,
        suspected_drug: susp,
        notes: notes
      })
    });
    if (!res.ok) throw new Error("Failed to submit symptom");
    showToast("Thank you! Your symptom has been logged and sent to your doctor & PvPI safety.");
    if ($("pat-sym-name")) $("pat-sym-name").value = "";
    if ($("pat-sym-notes")) $("pat-sym-notes").value = "";
    await renderPatientPortal(state.selectedPatientId);
  } catch (err) {
    showToast(`Error reporting symptom: ${err.message}`, true);
  }
}

// ---------------------------------------------------------------------------
// 16. Initialization & Event Listeners
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  // 1. Initialise form inputs
  if ($("input-key-ph")) $("input-key-ph").value = state.phKey;
  if ($("input-key-reg")) $("input-key-reg").value = state.regKey;

  // 2. Setup Routing
  window.addEventListener("hashchange", handleRouting);
  handleRouting();

  // 3. Connect SSE Stream for Pharmacist by default
  connectStream("ph", state.phKey);

  // 4. Initial telemetry & demo status poll
  refreshMetrics();
  setInterval(refreshMetrics, 3000);
  setInterval(pollDemoStatus, 2500);

  // 5. Sidebar toggle
  $("btn-toggle-sidebar")?.addEventListener("click", () => {
    state.isSidebarCollapsed = !state.isSidebarCollapsed;
    $("app-sidebar")?.classList.toggle("collapsed", state.isSidebarCollapsed);
  });

  // 6. Role Switchers
  $("role-btn-ph")?.addEventListener("click", () => setRole("pharmacist"));
  $("role-btn-reg")?.addEventListener("click", () => setRole("regulator"));
  $("role-btn-doc")?.addEventListener("click", () => setRole("doctor"));
  $("role-btn-pat")?.addEventListener("click", () => setRole("patient"));

  // 6b. Clinical Listeners
  $("doc-doctor-select")?.addEventListener("change", (e) => { state.selectedDoctorId = e.target.value; });
  $("doc-patient-select")?.addEventListener("change", (e) => { loadPatientDetails(e.target.value); });
  $("btn-doc-add-drug")?.addEventListener("click", addPrescriptionItem);
  $("btn-doc-commit-encounter")?.addEventListener("click", commitDoctorEncounter);
  $("btn-doc-reset-form")?.addEventListener("click", resetDoctorConsultationForm);
  $("doc-rx-drug-select")?.addEventListener("change", handleDrugSelectionChange);
  $("doc-chief-complaint")?.addEventListener("input", debouncePrecheck);
  $("doc-rx-indication")?.addEventListener("input", debouncePrecheck);

  $("pat-patient-select")?.addEventListener("change", (e) => { state.selectedPatientId = e.target.value; renderPatientPortal(e.target.value); });
  $("btn-pat-submit-symptom")?.addEventListener("click", submitPatientSymptom);

  // 7. Demonstration Actions
  $("btn-quick-demo")?.addEventListener("click", startDemoReplay);
  $("btn-overview-run-demo")?.addEventListener("click", startDemoReplay);
  $("btn-monitoring-demo")?.addEventListener("click", startDemoReplay);
  $("btn-launch-demo-monitoring")?.addEventListener("click", startDemoReplay);

  // 8. Modals
  $("btn-open-settings")?.addEventListener("click", () => { $("modal-settings").hidden = false; });
  $("btn-close-settings")?.addEventListener("click", () => { $("modal-settings").hidden = true; });
  $("settings-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    state.phKey = $("input-key-ph").value.trim() || DEMO_PH_KEY;
    state.regKey = $("input-key-reg").value.trim() || DEMO_REG_KEY;
    $("modal-settings").hidden = true;
    showToast("API credentials updated successfully.");
    setRole(state.role);
  });
  $("btn-autofill-keys")?.addEventListener("click", () => {
    $("input-key-ph").value = DEMO_PH_KEY;
    $("input-key-reg").value = DEMO_REG_KEY;
    showToast("Reset to official benchmark demonstration keys.");
  });

  $("btn-banner-guide")?.addEventListener("click", () => { $("modal-guide").hidden = false; });
  $("btn-close-guide")?.addEventListener("click", () => { $("modal-guide").hidden = true; });
  $("btn-guide-close-foot")?.addEventListener("click", () => { $("modal-guide").hidden = true; });

  $("btn-close-alert-modal")?.addEventListener("click", () => { $("modal-alert-detail").hidden = true; });
  $("btn-modal-close-footer")?.addEventListener("click", () => { $("modal-alert-detail").hidden = true; });

  // 9. Alert Table Filter listeners
  $("alert-search")?.addEventListener("input", renderAlertsTable);
  $("alert-status-filter")?.addEventListener("change", renderAlertsTable);
  $("btn-refresh-alerts")?.addEventListener("click", () => loadAlerts());

  // 10. Signals filters
  $("signals-win-select")?.addEventListener("change", () => {
    if ($("signals-chart-card")) $("signals-chart-card").hidden = true;
    loadSignalsView();
  });
  $("signals-bh-checkbox")?.addEventListener("change", () => loadSignalsView());
  $("btn-close-chart")?.addEventListener("click", () => {
    if ($("signals-chart-card")) $("signals-chart-card").hidden = true;
  });

  // 11. Audit and System Refresh
  $("btn-refresh-audit")?.addEventListener("click", loadAuditView);
  $("btn-refresh-system")?.addEventListener("click", loadSystemView);

  // 12. Global Keyboard Handlers
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll(".modal-backdrop").forEach((m) => { m.hidden = true; });
    }
  });
});
