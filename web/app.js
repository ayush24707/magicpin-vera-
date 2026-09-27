/* ==========================================================================
   Vera Elite — Console
   Zero-build ES module. Talks to the FastAPI service on the same origin.
   ========================================================================== */

const API = "/v1/dashboard";

/* --------------------------------------------------------------------------
   Utilities
   -------------------------------------------------------------------------- */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

const num = (n) => Number(n ?? 0).toLocaleString("en-IN");
const pct = (n, d = 1) => `${(Number(n ?? 0) * 100).toFixed(d)}%`;
const ms = (n) => `${Number(n ?? 0) < 10 ? Number(n ?? 0).toFixed(2) : Math.round(Number(n ?? 0))} ms`;

const initials = (name) => String(name || "?")
  .replace(/^(dr|mr|mrs|ms)\.?\s+/i, "")
  .split(/[\s'&]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";

const relTime = (iso) => {
  if (!iso) return "";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 10) return "just now";
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};

const clockTime = (iso) => {
  try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
};

const titleCase = (s) => String(s || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/* --------------------------------------------------------------------------
   API
   -------------------------------------------------------------------------- */
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
  } catch {
    // A rejected fetch means the request never reached the server, which is the
    // one case that genuinely means "offline". Flip the pill straight away --
    // pingHealth() is never reached on this path because it runs only after a
    // successful response.
    markEngineOffline();
    throw new Error(`Cannot reach the engine at ${location.origin}. Is bot.py still running?`);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    // 502/503/504 are returned by a proxy or load balancer when the upstream
    // process is gone, so they mean "unreachable" just like a dead socket.
    // A 500 from a live engine is a real application error and is left alone.
    if (res.status === 502 || res.status === 503 || res.status === 504) {
      markEngineOffline();
      throw new Error(`Cannot reach the engine at ${location.origin}. Is bot.py still running?`);
    }
    const msg = data?.detail || data?.reason || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

/* --------------------------------------------------------------------------
   Toasts
   -------------------------------------------------------------------------- */
const ICONS = {
  ok: '<path d="m5 13 4 4L19 7"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
};

function toast(title, body = "", kind = "info", ttl = 4200) {
  const host = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast toast--${kind}`;
  el.innerHTML = `
    <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
         style="margin-top:2px;color:var(--${kind === "ok" ? "ok" : kind === "error" ? "danger" : "accent"})"
         aria-hidden="true">${ICONS[kind] || ICONS.info}</svg>
    <div class="toast__text">
      <div class="toast__title">${esc(title)}</div>
      ${body ? `<div class="toast__body">${esc(body)}</div>` : ""}
    </div>`;
  host.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .2s, transform .2s";
    el.style.opacity = "0";
    el.style.transform = "translateX(20px)";
    setTimeout(() => el.remove(), 220);
  }, ttl);
}

function busy(btn, on) {
  if (!btn) return;
  btn.classList.toggle("is-busy", on);
  btn.disabled = on;
}

/* --------------------------------------------------------------------------
   State
   -------------------------------------------------------------------------- */
const state = {
  view: "dashboard",
  overview: null,
  merchants: [],
  categories: [],
  categoryFilter: "",
  query: "",
  selectedMerchant: null,
  merchantDetail: null,
  conversationId: null,
  conversation: null,
  threadOpen: false,
  closed: false,
  poll: null,
};

/* --------------------------------------------------------------------------
   Theme
   -------------------------------------------------------------------------- */
function initTheme() {
  const saved = localStorage.getItem("vera-theme");
  const prefersLight = window.matchMedia?.("(prefers-color-scheme: light)").matches;
  document.documentElement.dataset.theme = saved || (prefersLight ? "light" : "dark");
  updateThemeMeta();
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("vera-theme", next);
  updateThemeMeta();
}

function updateThemeMeta() {
  const dark = document.documentElement.dataset.theme === "dark";
  $('meta[name="theme-color"]')?.setAttribute("content", dark ? "#08070f" : "#f4f3fa");
}

/* --------------------------------------------------------------------------
   Tabs
   -------------------------------------------------------------------------- */
function moveGlider() {
  const active = $(".tab.is-active");
  const glider = $(".tabs__glider");
  if (!active || !glider || window.innerWidth <= 860) return;
  glider.style.width = `${active.offsetWidth}px`;
  glider.style.transform = `translateX(${active.offsetLeft - 4}px)`;
}

function setView(view) {
  state.view = view;
  $$(".tab").forEach((t) => {
    const on = t.dataset.view === view;
    t.classList.toggle("is-active", on);
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
  });
  $("#view-dashboard").hidden = view !== "dashboard";
  $("#view-simulator").hidden = view !== "simulator";
  moveGlider();
  window.scrollTo({ top: 0, behavior: "smooth" });

  if (view === "simulator" && !state.merchants.length) loadMerchants();
  if (view === "dashboard") refreshDashboard();
  managePolling();
}

function managePolling() {
  clearInterval(state.poll);
  // Health is polled on every tab so the pill recovers on its own once the
  // engine comes back; only the dashboard needs the heavier refresh.
  state.poll = setInterval(() => {
    if (document.hidden) return;
    pingHealth();
    if (state.view === "dashboard") refreshDashboard({ silent: true });
  }, 7000);
}

/* --------------------------------------------------------------------------
   Engine status
   -------------------------------------------------------------------------- */
async function pingHealth() {
  const el = $("#engineStatus");
  try {
    const res = await fetch("/v1/healthz");
    if (!res.ok) throw new Error();
    const data = await res.json();
    const total = Object.values(data.contexts_loaded || {}).reduce((a, b) => a + b, 0);
    const mins = Math.floor((data.uptime_seconds || 0) / 60);
    el.className = "status is-ok";
    el.innerHTML = `<span class="status__dot"></span><span class="status__text">online · ${num(total)} ctx · ${mins}m up</span>`;
  } catch {
    markEngineOffline();
  }
}

function markEngineOffline() {
  const el = $("#engineStatus");
  if (!el) return;
  el.className = "status is-down";
  el.innerHTML = `<span class="status__dot"></span><span class="status__text">engine offline</span>`;
}

/* --------------------------------------------------------------------------
   Dashboard
   -------------------------------------------------------------------------- */
async function refreshDashboard({ silent = false } = {}) {
  try {
    const data = await api("/overview");
    state.overview = data;
    renderKpis(data);
    renderPipeline(data);
    renderFeed(data);
    renderRubric(data);
    renderGuards(data);
    renderLatency(data);
    renderCohorts(data);
    if (!silent) pingHealth();
  } catch (err) {
    if (!silent) toast("Could not load telemetry", err.message, "error");
  }
}

function renderKpis(d) {
  const { contexts, activity, latency, quality } = d;
  const grid = $("#kpiGrid");

  const cards = [
    {
      label: "Contexts loaded", value: num(contexts.total), accent: "var(--brand)",
      meta: `${num(contexts.category)} cat · ${num(contexts.merchant)} mer · ${num(contexts.customer)} cust · ${num(contexts.trigger)} trg`,
      bar: Math.min(100, (contexts.total / 360) * 100),
    },
    {
      label: "Actions composed", value: num(activity.actions_composed), accent: "var(--accent)",
      meta: `${num(activity.replies_handled)} replies · ${num(activity.ticks)} ticks`,
      bar: Math.min(100, (activity.actions_composed / Math.max(1, activity.actions_composed + activity.replies_handled)) * 100),
    },
    {
      label: "Avg latency", value: ms(latency.avg_ms), accent: "var(--ok)",
      meta: `p95 ${ms(latency.p95_ms)} · budget ${num(latency.budget_ms)} ms`,
      bar: Math.max(1.5, Math.min(100, latency.budget_used_pct * 100)),
    },
    {
      label: "Guard interventions", value: num(activity.wait_events + activity.end_events), accent: "var(--warn)",
      meta: `${num(activity.wait_events)} backoffs · ${num(activity.end_events)} opt-outs`,
      bar: Math.min(100, (activity.qualification_free_rate || 0)),
    },
    {
      label: "Rubric score", value: quality.total ? `${quality.total}` : "—", accent: "var(--brand-2)",
      meta: quality.samples ? `${quality.verdict} · ${quality.samples} scored` : "no samples yet",
      bar: quality.pct || 0,
      suffix: quality.samples ? `<small> / 50</small>` : "",
    },
  ];

  grid.innerHTML = cards.map((c) => `
    <article class="kpi" style="--kpi-accent:${c.accent}">
      <div class="kpi__label">${esc(c.label)}</div>
      <div class="kpi__value">${c.value}${c.suffix || ""}</div>
      <div class="kpi__meta">${esc(c.meta)}</div>
      <div class="kpi__bar"><i style="width:${Math.max(2, c.bar).toFixed(1)}%"></i></div>
    </article>`).join("");
}

const NODE_META = [
  { key: "category", name: "CategoryContext", hint: "voice, taboos, catalog" },
  { key: "merchant", name: "MerchantContext", hint: "identity, KPIs, signals" },
  { key: "trigger", name: "TriggerContext", hint: "26 kinds, urgency" },
  { key: "customer", name: "CustomerContext", hint: "cohort, consent, history" },
];

const NODE_ICONS = [
  '<path d="M4 5h16M4 12h10M4 19h7"/>',
  '<path d="M3 21h18M5 21V8l7-5 7 5v13M9 21v-6h6v6"/>',
  '<path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12z"/>',
  '<circle cx="12" cy="8" r="3.2"/><path d="M5 20a7 7 0 0 1 14 0"/>',
];

function renderPipeline(d) {
  $("#pipelineNodes").innerHTML = NODE_META.map((n, i) => `
    <div class="node" title="${esc(n.hint)}">
      <span class="node__icon">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
             stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${NODE_ICONS[i]}</svg>
      </span>
      <span class="node__body">
        <span class="node__count">${num(d.contexts[n.key])}</span>
        <span class="node__name">${esc(n.name)}</span>
      </span>
    </div>`).join("");

  const latest = d.timeline.find((e) => e.kind === "action" || e.kind === "reply");
  $("#pipelineOutBody").textContent = latest
    ? (latest.body || latest.rationale || latest.title)
    : "awaiting activity…";
}

const FEED_ICONS = {
  action: '<path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12z"/>',
  reply: '<path d="M21 11.5a8.4 8.4 0 0 1-12 7.5L3 21l1.6-4.5A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4Z"/>',
  tick: '<path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 7v5l3 2"/>',
  context: '<path d="M4 7h16M4 12h10M4 17h7"/>',
  system: '<circle cx="12" cy="12" r="8"/><path d="M12 8h.01M11 12h1v4h1"/>',
};

function actionChip(e) {
  if (e.kind === "action") {
    return `<span class="chip chip--brand">${esc(e.cta || "binary")}</span>
            <span class="chip chip--soft">${esc(e.send_as || "vera")}</span>`;
  }
  if (e.kind === "reply") {
    const cls = e.action === "end" ? "chip--danger" : e.action === "wait" ? "chip--warn" : "chip--brand";
    return `<span class="chip ${cls}">${esc(e.action)}</span>`;
  }
  if (e.kind === "tick") return `<span class="chip chip--ok">${num(e.action_count || 0)} actions</span>`;
  if (e.kind === "context") return `<span class="chip chip--soft">${esc(e.scope || "")}</span>`;
  return "";
}

function renderFeed(d) {
  const feed = $("#feed");
  $("#feedCount").textContent = `${num(d.timeline.length)} event${d.timeline.length === 1 ? "" : "s"}`;

  if (!d.timeline.length) {
    feed.innerHTML = `
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true">
          <path d="M4 6h16M4 12h10M4 18h7"/>
        </svg>
        <p class="empty__title">No activity yet</p>
        <p class="empty__text">Load the seed dataset, then run a tick to watch the engine compose grounded messages.</p>
      </div>`;
    return;
  }

  feed.innerHTML = d.timeline.map((e) => {
    const icon = FEED_ICONS[e.kind] || FEED_ICONS.system;
    return `
      <article class="feedItem">
        <span class="feedItem__icon" data-kind="${esc(e.kind)}">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
               stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icon}</svg>
        </span>
        <div>
          <div class="feedItem__top">
            <span class="feedItem__title">${esc(e.title)}</span>
            <span class="feedItem__time" title="${esc(e.ts)}">${relTime(e.ts)}</span>
          </div>
          ${e.subtitle ? `<div class="feedItem__sub">${esc(e.subtitle)}</div>` : ""}
          ${e.body ? `<p class="feedItem__quote">${esc(e.body.slice(0, 240))}${e.body.length > 240 ? "…" : ""}</p>` : ""}
          ${e.rationale ? `<p class="feedItem__why">${esc(e.rationale)}</p>` : ""}
          ${(() => {
            const chips = actionChip(e) +
              (e.score ? `<span class="chip chip--mono">${e.score.total}/50</span>` : "") +
              (e.latency_ms != null ? `<span class="chip chip--mono">${ms(e.latency_ms)}</span>` : "");
            return chips ? `<div class="feedItem__tags">${chips}</div>` : "";
          })()}
        </div>
      </article>`;
  }).join("");
}

function renderRubric(d) {
  const q = d.quality;
  const names = {
    specificity: "Specificity & grounding",
    category_fit: "Category voice & taboo fit",
    merchant_fit: "Merchant personalisation",
    decision_quality: "Decision & routing",
    engagement: "Engagement compulsion & CTA",
  };

  const ring = $("#scoreRing");
  ring.style.setProperty("--pct", String(q.pct || 0));
  ring.style.setProperty("--ring-color", q.pct >= 80 ? "var(--ok)" : q.pct >= 60 ? "var(--brand)" : q.pct ? "var(--warn)" : "var(--text-3)");
  $("#scoreRingValue").textContent = q.samples ? Math.round(q.total) : "—";

  $("#rubric").innerHTML = Object.entries(names).map(([key, label]) => {
    const d2 = q.dimensions[key] || { score: 0 };
    return `
      <div class="rubricRow">
        <span class="rubricRow__name">${esc(label)}</span>
        <span class="rubricRow__score">${d2.score}<span style="color:var(--text-3)">/10</span></span>
        <span class="rubricRow__track"><i class="rubricRow__fill" style="width:${(d2.score / 10) * 100}%"></i></span>
      </div>`;
  }).join("") + (q.fact_coverage ? `
      <p class="rubricRow__note" style="margin-top:4px">
        Grounded anchors extracted: ${num(q.fact_coverage.numbers)} number${q.fact_coverage.numbers === 1 ? "" : "s"},
        ${num(q.fact_coverage.percentages)} percentage${q.fact_coverage.percentages === 1 ? "" : "s"},
        ${num(q.fact_coverage.prices)} price${q.fact_coverage.prices === 1 ? "" : "s"},
        ${num(q.fact_coverage.citations)} citation${q.fact_coverage.citations === 1 ? "" : "s"}.
      </p>` : "");
}

const GUARD_COPY = {
  auto_reply_guard: { armed: "No canned reply seen", idle: "Idle — no auto-reply intercepted" },
  hostile_exit:     { armed: "No opt-out received", idle: "Idle — all threads healthy" },
  intent_handoff:   { armed: "No commitment yet", idle: "Idle — no intent handoffs" },
  // Not a guard — a running total. Rendered in the neutral "metric" style.
  factual_anchor:   { metric: (n) => n ? `Rubric-scored, ${n} message${n === 1 ? "" : "s"}` : "Nothing composed yet" },
};

function renderGuards(d) {
  $("#guards").innerHTML = Object.entries(d.guard_matrix).map(([key, g]) => {
    const copy = GUARD_COPY[key] || { armed: "Active", idle: "Idle" };

    if (copy.metric) {
      const n = g.samples || 0;
      return `
        <div class="guard ${n ? "is-armed" : ""}">
          <span class="guard__icon">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              ${n ? '<path d="m5 13 4 4L19 7"/>' : '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2 2"/>'}
            </svg>
          </span>
          <span class="guard__body">
            <span class="guard__name">${esc(g.label)}</span>
            <span class="guard__state">${esc(copy.metric(n))}</span>
          </span>
          <span class="guard__count">${num(n)}</span>
        </div>`;
    }

    const armed = (g.fired || 0) > 0;
    return `
      <div class="guard ${armed ? "is-armed" : ""}">
        <span class="guard__icon">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            ${armed ? '<path d="m5 13 4 4L19 7"/>' : '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2 2"/>'}
          </svg>
        </span>
        <span class="guard__body">
          <span class="guard__name">${esc(g.label)}</span>
          <span class="guard__state">${esc(armed ? copy.armed : copy.idle)}</span>
        </span>
        <span class="guard__count">${num(g.fired || 0)}</span>
      </div>`;
  }).join("");
}

function sparkline(series) {
  const W = 100, H = 100;
  if (!series || series.length < 2) {
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
      <line x1="0" y1="${H - 1}" x2="${W}" y2="${H - 1}" stroke="currentColor" stroke-opacity=".22" stroke-width="1" vector-effect="non-scaling-stroke"/>
    </svg>`;
  }
  const max = Math.max(...series, 0.001);
  const step = W / (series.length - 1);
  const pts = series.map((v, i) => [i * step, H - (v / max) * (H - 10) - 5]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="lg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--brand)" stop-opacity=".42"/>
      <stop offset="100%" stop-color="var(--brand)" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="${line} L${W},${H} L0,${H} Z" fill="url(#lg)"/>
    <path d="${line}" fill="none" stroke="var(--brand-2)" stroke-width="1.6"
          vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function renderLatency(d) {
  const l = d.latency;
  $("#latAvg").textContent = l.samples ? `${ms(l.avg_ms)} avg` : "no samples";
  $("#latSpark").innerHTML = sparkline(l.series);
  $("#latStats").innerHTML = [
    ["p50", ms(l.p50_ms)],
    ["p95", ms(l.p95_ms)],
    ["max", ms(l.max_ms)],
    ["samples", num(l.samples)],
  ].map(([k, v]) => `
    <div class="latency__stat"><dt>${k}</dt><dd>${v}</dd></div>`).join("");
}

function bench(avg, peer, isPct) {
  if (!peer) return `<span class="bench__val" style="color:var(--text-3)">—</span>`;
  const ratio = avg / peer;
  const width = Math.min(100, ratio * 55);
  const marker = Math.min(100, 55);
  const val = isPct ? pct(avg) : num(avg);
  return `
    <span class="bench">
      <span class="bench__track">
        <span class="bench__fill" style="width:${width}%"></span>
        <span class="bench__peer" style="left:${marker}%" title="peer median"></span>
      </span>
      <span class="bench__val">${val}</span>
    </span>`;
}

function renderCohorts(d) {
  const body = $("#cohortBody");
  if (!d.cohorts.length) {
    body.innerHTML = `<tr><td colspan="7" class="table__empty">
      No category contexts loaded yet — use “Load seed dataset”.
    </td></tr>`;
    return;
  }
  body.innerHTML = d.cohorts.map((c) => `
    <tr>
      <td>
        <div class="table__name">${esc(titleCase(c.display_name))}</div>
        <div class="table__slug">${esc(c.slug)}</div>
      </td>
      <td><span class="chip chip--soft">${esc(c.tone || "—")}</span></td>
      <td class="num">${num(c.merchants)}</td>
      <td class="num">${num(c.customers)}</td>
      <td class="num">${num(c.triggers)}</td>
      <td>${bench(c.avg_views, c.peer_views, false)}</td>
      <td>${bench(c.avg_ctr, c.peer_ctr, true)}</td>
    </tr>`).join("");
}

/* --------------------------------------------------------------------------
   Dashboard actions
   -------------------------------------------------------------------------- */
async function doSeed() {
  const btn = $("#seedBtn");
  busy(btn, true);
  try {
    const res = await api("/seed", { method: "POST", body: {} });
    toast(
      "Seed dataset loaded",
      `${num(res.total)} contexts in ${res.elapsed_ms} ms — ${Object.entries(res.loaded).map(([k, v]) => `${v} ${k}`).join(", ")}`,
      "ok"
    );
    state.merchants = [];
    await Promise.all([refreshDashboard(), pingHealth()]);
    if (state.view === "simulator") loadMerchants();
  } catch (err) {
    toast("Seed failed", err.message, "error");
  } finally {
    busy(btn, false);
  }
}

async function doTick() {
  const btn = $("#tickBtn");
  busy(btn, true);
  try {
    const res = await api("/tick", { method: "POST", body: { limit: 20 } });
    toast(
      "Tick complete",
      `${res.actions.length} action(s) from ${res.trigger_count} trigger(s) in ${res.elapsed_ms ?? ms(res.latency_ms)}`,
      res.actions.length ? "ok" : "info"
    );
    await refreshDashboard();
  } catch (err) {
    toast("Tick failed", err.message, "error");
  } finally {
    busy(btn, false);
  }
}

async function doReset() {
  if (!confirm("Clear all loaded contexts, suppression keys and transcripts?")) return;
  const btn = $("#resetBtn");
  busy(btn, true);
  try {
    await api("/reset", { method: "POST" });
    state.merchants = [];
    state.selectedMerchant = null;
    state.merchantDetail = null;
    state.conversation = null;
    state.conversationId = null;
    state.threadOpen = false;
    state.closed = false;
    renderMerchantList();
    resetChat();
    setSimulatorEnabled(false);
    await refreshDashboard();
    toast("Engine cleared", "All in-memory state dropped", "ok");
  } catch (err) {
    toast("Reset failed", err.message, "error");
  } finally {
    busy(btn, false);
  }
}

/* --------------------------------------------------------------------------
   Simulator — merchant picker
   -------------------------------------------------------------------------- */
async function loadMerchants() {
  const list = $("#merchantList");
  list.innerHTML = `<div class="empty"><p class="empty__text">Loading merchants…</p></div>`;
  try {
    const [m, c] = await Promise.all([
      api(`/merchants?${new URLSearchParams({ ...(state.query ? { q: state.query } : {}), ...(state.categoryFilter ? { category: state.categoryFilter } : {}) })}`),
      state.categories.length ? { categories: state.categories } : api("/categories"),
    ]);
    state.merchants = m.merchants;
    state.categories = c.categories;
    renderCategoryChips();
    renderMerchantList();
  } catch (err) {
    list.innerHTML = `<div class="empty">
      <p class="empty__title">No merchants available</p>
      <p class="empty__text">Load the seed dataset from the Dashboard first. (${esc(err.message)})</p>
    </div>`;
  }
}

function renderCategoryChips() {
  const host = $("#categoryChips");
  const all = [{ slug: "", display_name: "All" }, ...state.categories];
  host.innerHTML = all.map((c) => `
    <button class="chip chip--filter ${state.categoryFilter === c.slug ? "is-on" : ""}"
            data-slug="${esc(c.slug)}" type="button">
      ${esc(titleCase(c.display_name))}${c.merchants ? ` · ${c.merchants}` : ""}
    </button>`).join("");
}

function renderMerchantList() {
  const list = $("#merchantList");
  $("#merchantCount").textContent = num(state.merchants.length);
  if (!state.merchants.length) {
    // An empty picker means one of two very different things. Categories only
    // exist once contexts are loaded, so no categories means nothing is loaded
    // yet -- telling that user to "adjust the filter" is a dead end.
    list.innerHTML = state.categories.length
      ? `<div class="empty">
          <p class="empty__title">Nothing matches</p>
          <p class="empty__text">Adjust the search or category filter.</p>
        </div>`
      : `<div class="empty">
          <p class="empty__title">No merchants loaded</p>
          <p class="empty__text">The engine has no contexts yet. Load the seed dataset to populate this picker.</p>
          <button class="btn btn--primary btn--sm" data-act="seed" type="button">Load seed dataset</button>
        </div>`;
    return;
  }
  list.innerHTML = state.merchants.map((m) => `
    <button class="mItem ${state.selectedMerchant?.merchant_id === m.merchant_id ? "is-on" : ""}"
            data-id="${esc(m.merchant_id)}" role="option"
            aria-selected="${state.selectedMerchant?.merchant_id === m.merchant_id}" type="button">
      <span class="avatar">${esc(initials(m.name))}</span>
      <span style="min-width:0">
        <span class="mItem__name">${esc(m.name)}</span>
        <span class="mItem__meta">${esc(m.locality || m.city || "—")} · ${esc(titleCase(m.category_slug))}</span>
        <span class="mItem__stats">
          <span>${num(m.views)} views</span><span>·</span><span>${pct(m.ctr)} CTR</span>
        </span>
      </span>
    </button>`).join("");
}

async function selectMerchant(id) {
  const m = state.merchants.find((x) => x.merchant_id === id);
  if (!m) return;
  state.selectedMerchant = m;
  state.threadOpen = false;
  state.closed = false;
  renderMerchantList();
  setSimulatorEnabled(false);

  $("#chatTitle").textContent = m.name;
  $("#chatMeta").textContent = `${m.locality || m.city} · ${titleCase(m.category_name || m.category_slug)} · ${num(m.views)} views · ${pct(m.ctr)} CTR`;
  $("#chatAvatar").textContent = initials(m.name);

  const sel = $("#triggerSelect");
  sel.disabled = true;
  sel.innerHTML = `<option value="">Loading triggers…</option>`;

  try {
    const [detail, trig] = await Promise.all([
      api(`/merchants/${encodeURIComponent(id)}`),
      api(`/triggers?merchant_id=${encodeURIComponent(id)}`),
    ]);
    state.merchantDetail = detail;

    const options = [`<option value="">Cold open — profile audit</option>`];
    trig.triggers.slice(0, 40).forEach((t) => {
      const who = t.customer_id ? " · customer" : "";
      options.push(`<option value="${esc(t.id)}">${esc(titleCase(t.kind))} · urgency ${t.urgency}${esc(who)}</option>`);
    });
    sel.innerHTML = options.join("");
    sel.disabled = false;
    setSimulatorEnabled(true);
    renderTrace(null);
  } catch (err) {
    sel.innerHTML = `<option value="">Unavailable</option>`;
    toast("Could not load merchant context", err.message, "error");
  }
}

function setSimulatorEnabled(on) {
  // The composer and the scenario shortcuts only make sense once a thread
  // exists — a merchant can be picked long before "Open thread" is pressed.
  const live = !!on && state.threadOpen && !state.closed;
  $("#openChatBtn").disabled = !on;
  $("#messageInput").disabled = !live;
  $("#sendBtn").disabled = !live;
  $$("#quickReplies .quick").forEach((b) => { b.disabled = !live; });
}

/* --------------------------------------------------------------------------
   Simulator — thread
   -------------------------------------------------------------------------- */
function resetChat() {
  $("#chatLog").innerHTML = `<div class="empty">
    <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 11.5a8.4 8.4 0 0 1-12 7.5L3 21l1.6-4.5A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4Z"/>
    </svg>
    <p class="empty__title">No thread open</p>
    <p class="empty__text">Pick a merchant, choose an opener trigger, then press “Open thread”.</p>
  </div>`;
}

function chatDateChip() {
  return `<div class="chat__day">Today</div>`;
}

function bubbleHtml(turn) {
  const out = turn.role === "vera";
  const who = out ? (turn.send_as === "merchant_on_behalf" ? "Merchant (on behalf)" : "Vera") : "Merchant";
  const lat = turn.latency_ms != null ? ` · ${ms(turn.latency_ms)}` : "";
  return `
    <div class="msg msg--${out ? "out" : "in"}">
      <div class="bubble">${esc(turn.message)}</div>
      <div class="msg__meta">
        <span class="msg__who">${esc(who)}</span>
        <span>${clockTime(turn.ts)}${lat}</span>
      </div>
    </div>`;
}

function syslineHtml(turn) {
  if (turn.source === "guard_core") return "";   // engine mirror, not a decision
  if (turn.send_as === "merchant_on_behalf") {
    return `<div class="sysline sysline--sendas">↗ sent as merchant_on_behalf</div>`;
  }
  return "";
}

/* Rendered *after* the merchant's own bubble — the engine only decides once
   it has read what the merchant said. */
function syslineAfterHtml(turn) {
  if (turn.source === "guard_core") return "";
  if (turn.action === "wait") {
    const hours = (turn.wait_seconds || 0) / 3600;
    return `<div class="sysline sysline--wait">⏸ Backoff ${hours % 1 ? hours.toFixed(1) : hours} h — no message sent (auto-reply guard)</div>`;
  }
  if (turn.action === "end") {
    return `<div class="sysline sysline--end">⛔ Conversation closed · suppression key ${esc(turn.suppression_key || "set")}</div>`;
  }
  return "";
}

function renderChat() {
  const log = $("#chatLog");
  const turns = state.conversation?.turns || [];
  if (!turns.length) { resetChat(); return; }

  let html = chatDateChip();
  for (const t of turns) {
    const pre = syslineHtml(t);
    if (pre) html += pre;
    if (t.message) html += bubbleHtml(t);
    const post = syslineAfterHtml(t);
    if (post) html += post;
  }
  if (state.closed) {
    html += `<div class="sysline sysline--end">Thread closed — press “New thread” to keep testing</div>`;
  }
  log.innerHTML = html;
  log.scrollTop = log.scrollHeight;
}

function showTyping() {
  const log = $("#chatLog");
  const el = document.createElement("div");
  el.className = "msg msg--out";
  el.id = "typingIndicator";
  el.innerHTML = `<div class="bubble typing"><i></i><i></i><i></i></div>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function hideTyping() { $("#typingIndicator")?.remove(); }

function renderTrace(turn) {
  const host = $("#tracePanel");
  const m = state.selectedMerchant;
  const chip = $("#traceTurn");

  if (!turn) {
    chip.textContent = "—";
    host.innerHTML = `
      <div class="empty">
        <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h10M4 18h7"/></svg>
        <p class="empty__title">Nothing traced yet</p>
        <p class="empty__text">Open a thread to see which guard stage fires on every turn.</p>
      </div>
      ${m ? contextSnapshotHtml() : ""}`;
    return;
  }

  const decisionTurn = turn.role === "merchant" ? turn : state.conversation.turns.find((t) => t.role === "merchant" && t.turn === turn.turn) || turn;
  const facts = decisionTurn.grounding || {};
  const factChips = [
    ...(facts.numbers || []).map((f) => `<span class="fact">${esc(f)}</span>`),
    ...(facts.percentages || []).map((f) => `<span class="fact">${esc(f)}</span>`),
    ...(facts.prices || []).map((f) => `<span class="fact">${esc(f)}</span>`),
    ...(facts.citations || []).map((f) => `<span class="fact fact--cite">${esc(f)}</span>`),
  ];
  const uniqueFacts = [...new Set(factChips)].slice(0, 22);

  chip.textContent = `turn ${turn.turn}`;
  chip.dataset.action = turn.action || "";

  host.innerHTML = `
    <div class="traceBlock">
      <div class="traceVerdict">
        <span class="traceVerdict__action" data-action="${esc(turn.action)}">${esc(turn.action)}</span>
        <div class="traceVerdict__meta">
          turn ${turn.turn} · ${turn.latency_ms != null ? ms(turn.latency_ms) : "—"}<br>
          ${turn.cta ? `CTA ${esc(turn.cta)}` : "no CTA"}
        </div>
      </div>
      ${turn.wait_seconds ? `<span class="chip chip--warn">backoff ${num(turn.wait_seconds)} s</span>` : ""}
      ${turn.suppression_key ? `<span class="chip chip--mono">${esc(turn.suppression_key)}</span>` : ""}
    </div>

    <div class="traceBlock">
      <p class="traceBlock__title">Pipeline trace</p>
      ${(turn.trace || []).map((s) => `
        <div class="stage" data-status="${esc(s.status)}">
          <span class="stage__dot"></span>
          <span>
            <span class="stage__name">${esc(s.layer)}</span>
            <span class="stage__detail">${esc(s.detail)}</span>
          </span>
        </div>`).join("")}
    </div>

    ${uniqueFacts.length ? `
    <div class="traceBlock">
      <p class="traceBlock__title">Grounded anchors extracted</p>
      <div class="facts">${uniqueFacts.join("")}</div>
    </div>` : ""}

    <div class="traceBlock">
      <p class="traceBlock__title">Rationale</p>
      <p class="stage__detail" style="font-size:12.5px;color:var(--text-2)">${esc(turn.rationale || "—")}</p>
    </div>

    ${decisionTurn.contract ? `
    <div class="traceBlock">
      <details class="json">
        <summary>Raw guard-core response (POST /v1/reply contract)</summary>
        <pre>${esc(JSON.stringify(decisionTurn.contract, null, 2))}</pre>
      </details>
    </div>` : ""}

    ${state.merchantDetail ? contextSnapshotHtml() : ""}`;
}

function contextSnapshotHtml() {
  const d = state.merchantDetail;
  if (!d) return "";
  const s = d.summary, c = d.category_context, p = d.merchant_context;
  const perf = p.performance || {}, agg = p.customer_aggregate || {}, peer = c.peer_stats || {};
  const active = (p.offers || []).filter((o) => o.status === "active").map((o) => o.title);
  const layers = [
    {
      name: "1 · CategoryContext", val: c.display_name || s.category_slug,
      body: `Voice <b>${esc(c.voice?.tone || "—")}</b> · ${(c.voice?.vocab_taboo || []).length} taboo filter(s) · peer median ${pct(peer.avg_ctr)} CTR`,
    },
    {
      name: "2 · MerchantContext", val: `${num(perf.views)} views`,
      body: `${num(perf.calls)} calls · ${pct(perf.ctr)} CTR · ${num(agg.total_unique_ytd || 0)} unique customers YTD · ${(p.signals || []).length} signal(s)`,
    },
    {
      name: "3 · TriggerContext", val: `${(d.triggers || []).length} armed`,
      body: (d.triggers || []).slice(0, 3).map((t) => `${esc(titleCase(t.kind))} (u${t.urgency})`).join(" · ") || "none loaded",
    },
    {
      name: "4 · CustomerContext", val: `${(d.customers || []).length} linked`,
      body: (d.customers || []).slice(0, 3).map((cu) => `${esc(cu.identity?.name || "?")} · ${esc(cu.state || "—")}`).join(" · ") || "none linked",
    },
  ];
  return `
    <div class="traceBlock">
      <p class="traceBlock__title">4-context snapshot</p>
      ${layers.map((l) => `
        <div class="ctxLayer">
          <div class="ctxLayer__top">
            <span class="ctxLayer__name">${esc(l.name)}</span>
            <span class="ctxLayer__val">${esc(String(l.val))}</span>
          </div>
          <div class="ctxLayer__body">${l.body}</div>
        </div>`).join("")}
      ${active.length ? `<div class="ctxLayer"><div class="ctxLayer__top"><span class="ctxLayer__name">Active offers</span></div>
        <div class="ctxLayer__body">${active.map(esc).join(" · ")}</div></div>` : ""}
    </div>`;
}

async function openThread() {
  const m = state.selectedMerchant;
  if (!m) return;
  const btn = $("#openChatBtn");
  busy(btn, true);
  state.conversationId = `conv_console_${Date.now().toString(36)}`;
  state.conversation = { turns: [] };
  state.threadOpen = false;
  state.closed = false;
  renderChat();
  showTyping();
  try {
    const res = await api("/simulate", {
      method: "POST",
      body: {
        conversation_id: state.conversationId,
        merchant_id: m.merchant_id,
        trigger_id: $("#triggerSelect").value || null,
        message: "",
      },
    });
    state.conversation = res;
    state.threadOpen = true;
    hideTyping();
    renderChat();
    renderTrace(res.turn);
    setSimulatorEnabled(true);
    toast("Thread opened", `Opener: ${res.turn.source || "grounded"}`, "ok", 2600);
  } catch (err) {
    hideTyping();
    toast("Could not open thread", err.message, "error");
  } finally {
    busy(btn, false);
    setSimulatorEnabled(true);
  }
}

async function sendMessage(text) {
  const m = state.selectedMerchant;
  const input = $("#messageInput");
  const message = (text ?? input.value).trim();
  if (!m || !state.threadOpen || !message || state.closed) return;

  input.value = "";
  input.style.height = "auto";

  // Optimistic echo
  state.conversation = state.conversation || { turns: [] };
  state.conversation.turns.push({ role: "merchant", message, ts: new Date().toISOString(), action: "pending" });
  renderChat();
  showTyping();

  const btn = $("#sendBtn");
  busy(btn, true);
  try {
    const res = await api("/simulate", {
      method: "POST",
      body: { conversation_id: state.conversationId, merchant_id: m.merchant_id, message },
    });
    state.conversation = res;
    state.closed = !!res.closed;
    hideTyping();
    renderChat();
    const decision = res.turn.role === "merchant" ? res.turn : res.turns.find((t) => t.role === "merchant" && t.turn === res.turn.turn);
    renderTrace(decision);
    if (state.closed) {
      toast("Conversation closed", "Opt-out honoured — suppression key recorded", "info");
    }
  } catch (err) {
    hideTyping();
    state.conversation.turns.pop();
    renderChat();
    toast("Engine error", err.message, "error");
  } finally {
    busy(btn, false);
    // busy() force-enables the button, so re-assert the closed-thread state.
    setSimulatorEnabled(!!m);
    if (!state.closed) input.focus();
  }
}

const SCENARIOS = [
  { label: "Auto-reply (canned)", text: "Thank you for contacting us! Our team will respond shortly." },
  { label: "“Let's do it”", text: "Ok lets do it. Whats next?" },
  { label: "Ask for the abstract", text: "Can you send me the abstract PDF for that study?" },
  { label: "Opt out", text: "Stop messaging me. This is useless spam." },
  { label: "Ask a question", text: "What is the status of my listing?" },
];

function renderQuickReplies() {
  $("#quickReplies").innerHTML = SCENARIOS.map((s, i) => `
    <button class="quick" data-idx="${i}" type="button" disabled title="${esc(s.text)}">${esc(s.label)}</button>`).join("");
}

/* --------------------------------------------------------------------------
   Wiring
   -------------------------------------------------------------------------- */
function init() {
  initTheme();
  moveGlider();
  renderQuickReplies();
  resetChat();

  // Tabs
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
    tab.addEventListener("keydown", (e) => {
      const tabs = $$(".tab");
      const i = tabs.indexOf(tab);
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        next.focus();
        setView(next.dataset.view);
      }
    });
  });

  // Theme / refresh
  $("#themeBtn").addEventListener("click", toggleTheme);
  $("#refreshBtn").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    b.classList.add("is-busy");
    await refreshDashboard();
    await pingHealth();
    b.classList.remove("is-busy");
  });

  // Dashboard actions
  $("#seedBtn").addEventListener("click", doSeed);
  $("#tickBtn").addEventListener("click", doTick);
  $("#resetBtn").addEventListener("click", doReset);

  // Merchant picker
  $("#merchantList").addEventListener("click", (e) => {
    if (e.target.closest('[data-act="seed"]')) return doSeed();
    const btn = e.target.closest(".mItem");
    if (btn) selectMerchant(btn.dataset.id);
  });
  $("#categoryChips").addEventListener("click", (e) => {
    const btn = e.target.closest(".chip--filter");
    if (!btn) return;
    state.categoryFilter = btn.dataset.slug;
    loadMerchants();
  });

  let searchTimer;
  $("#merchantSearch").addEventListener("input", (e) => {
    state.query = e.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadMerchants, 220);
  });

  // Chat
  $("#openChatBtn").addEventListener("click", openThread);
  $("#newChatBtn").addEventListener("click", () => {
    state.conversationId = null;
    state.conversation = null;
    state.threadOpen = false;
    state.closed = false;
    resetChat();
    renderTrace(null);
    setSimulatorEnabled(!!state.selectedMerchant);
    toast("New thread ready", state.selectedMerchant ? `Open one for ${state.selectedMerchant.name}` : "Pick a merchant first", "info", 2400);
  });
  $("#sendBtn").addEventListener("click", () => sendMessage());
  $("#messageInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $("#messageInput").addEventListener("input", (e) => {
    e.target.style.height = "auto";
    e.target.style.height = `${Math.min(132, e.target.scrollHeight)}px`;
  });
  $("#quickReplies").addEventListener("click", (e) => {
    const btn = e.target.closest(".quick");
    if (!btn || btn.disabled) return;
    sendMessage(SCENARIOS[Number(btn.dataset.idx)].text);
  });

  // Keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape") { document.activeElement?.blur(); return; }
    if (typing) return;
    if (e.key === "1") setView("dashboard");
    if (e.key === "2") setView("simulator");
    if (e.key.toLowerCase() === "r") $("#refreshBtn").click();
    if (e.key.toLowerCase() === "t") toggleTheme();
    if (e.key === "/" && state.view === "simulator") { e.preventDefault(); $("#merchantSearch").focus(); }
  });

  window.addEventListener("resize", moveGlider);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshDashboard({ silent: true }); });

  // First paint
  refreshDashboard();
  pingHealth();
  managePolling();
  if (!state.merchants.length) loadMerchants();
}

document.addEventListener("DOMContentLoaded", init);
