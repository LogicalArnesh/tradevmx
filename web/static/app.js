/* TradeVMx console client - no framework, no build step. */
(() => {
  "use strict";

  const TOKEN = (document.querySelector('meta[name="tradevmx-token"]') || {}).content || "";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value === null || value === undefined ? "" : value)
    .replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

  const num = (value, digits = 2) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    return parsed.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  };

  const pct = (value, digits = 2) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    return `${parsed >= 0 ? "+" : ""}${parsed.toFixed(digits)}%`;
  };

  const clockFormat = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });

  const VIEW_META = {
    dashboard: ["Market pulse", "Live tape, bias and the autonomous sweep."],
    settings: ["Configuration", "Keys, watchlist and risk thresholds for this PC."],
    activity: ["Activity", "Everything the sentinel has done recently."],
  };

  const TESTABLE = { telegram: "Telegram", email: "Email", groq: "Groq" };

  let state = null;
  let settings = null;
  let chartState = null;
  let settings = null;
  let clockSkew = 0;
  let currentView = "dashboard";
  let busy = false;

  /* ------------------------------------------------------------- helpers */
  async function api(path, options = {}) {
    const response = await fetch(path, {
      method: options.method || "GET",
      headers: {
        "Content-Type": "application/json",
        "X-TradeVMx-Token": TOKEN,
        ...(options.headers || {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
    });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const error = new Error((payload && payload.error) || `Request failed (${response.status})`);
      error.fields = payload && payload.fields ? payload.fields : null;
      throw error;
    }
    return payload;
  }

  function toast(message, kind = "info") {
    const host = $("#toasts");
    if (!host) return;
    const node = document.createElement("div");
    node.className = "toast" + (kind === "error" ? " toast-error" : "");
    node.textContent = message;
    host.appendChild(node);
    window.setTimeout(() => node.remove(), 5400);
  }

  function setBusy(button, label) {
    if (!button) return () => {};
    const original = button.textContent;
    button.disabled = true;
    if (label) button.textContent = label;
    return () => {
      button.disabled = false;
      button.textContent = original;
    };
  }

  /* -------------------------------------------------------------- render */
  function renderClock() {
    const now = new Date(Date.now() - clockSkew);
    const clock = $("#clock-time");
    if (clock) clock.textContent = clockFormat.format(now);
  }

  function renderReadiness(data) {
    const host = $("#readiness");
    if (!host) return;
    const items = [
      ["Telegram", data.readiness.telegram],
      ["Email", data.readiness.email],
      ["Groq", data.readiness.groq],
      ["Voice", data.readiness.voice],
    ];
    host.innerHTML = items
      .map(([label, on]) => `<span class="chip ${on ? "on" : "off"}"><i></i>${esc(label)}</span>`)
      .join("");
  }

  function nextSweepLabel(data) {
    const job = (data.sentinel.schedule || []).find((entry) => entry.id === "intraday_scan");
    return job && job.next_label ? job.next_label : "—";
  }

  function renderStats(data) {
    const host = $("#stats");
    if (!host) return;
    const stats = [
      ["Instruments", data.market.scanned ? String(data.market.scanned) : "0", `${data.sentinel.timeframe} sweep`],
      ["Actionable", String(data.market.actionable), `severity ≥ ${data.sentinel.alert_floor}`],
      ["Alerts sent", String(data.sentinel.last_alert_count), "last sweep"],
      ["Next sweep", nextSweepLabel(data), data.sentinel.running ? "sentinel armed" : "sentinel idle"],
    ];
    host.innerHTML = stats
      .map(([label, value, note]) => `
        <div class="stat">
          <div class="stat-label">${esc(label)}</div>
          <div class="stat-value">${esc(value)}</div>
          <div class="stat-note">${esc(note)}</div>
        </div>`)
      .join("");
  }

  function signalChip(signal) {
    const direction = String(signal.direction || "").toLowerCase();
    return `<span class="sig ${esc(direction)}">${esc(signal.label)}<em>${esc(signal.timeframe)} · ${esc(signal.severity)}/5</em></span>`;
  }

  function renderTape(data) {
    const host = $("#tape");
    if (!host) return;
    const rows = data.market.rows || [];
    if (!rows.length) {
      host.innerHTML = `<div class="empty">No sweep yet. Press <b>Scan now</b>, or arm the sentinel —
        the first sweep runs immediately when the market is open.</div>`;
      return;
    }
    host.innerHTML = rows.map((row) => {
      const biasClass = row.bias === "BULLISH" ? "bull" : row.bias === "BEARISH" ? "bear" : "neutral";
      const signals = (row.signals || []).length
        ? row.signals.map(signalChip).join("")
        : `<span class="sig muted">no setup</span>`;
      const verdict = row.verdict
        ? `<span class="verdict">${esc(row.verdict)}${row.risk_reward ? ` · 1:${esc(Number(row.risk_reward).toFixed(2))}` : ""}</span>`
        : "";
      return `<article class="row">
        <div class="row-id">
          <strong>${esc(row.name)}</strong>
          <span class="muted mono">${esc(row.symbol)}${row.stale ? " · stale" : ""}</span>
        </div>
        <div class="row-price">
          <span class="mono price">${num(row.cmp)}</span>
          <span class="mono ${row.change_pct >= 0 ? "up" : "down"}">${pct(row.change_pct)}</span>
        </div>
        <div class="row-bias">
          <span class="bias ${biasClass}">${esc(row.bias)}</span>
          <span class="muted small">${esc(row.structure)} · VWAP ${pct(row.vwap_distance_pct)}</span>
        </div>
        <div class="row-signals">${signals}</div>
        <div class="row-actions">
          ${verdict}
          <button class="btn small" type="button" data-report="${esc(row.name)}">PDF</button>
        </div>
      </article>`;
    }).join("");
  }

  function renderEvents(data) {
    const host = $("#events");
    const counter = $("#feed-count");
    if (!host) return;
    const events = data.events || [];
    if (counter) counter.textContent = String(events.length);
    if (!events.length) {
      host.innerHTML = `<li class="empty">Quiet. Signals and system events appear here.</li>`;
      return;
    }
    host.innerHTML = events.map((event) => `
      <li class="event level-${esc(event.level)}">
        <div class="event-top">
          <span class="mono muted small">${esc(event.time)}</span>
          <span class="ev-title">${esc(event.title)}</span>
        </div>
        ${event.detail ? `<p>${esc(event.detail)}</p>` : ""}
      </li>`).join("");
  }

  function renderAll(data) {
    state = data;
    clockSkew = Date.now() - Date.parse(data.clock.ist);
    renderClock();
    renderReadiness(data);
    renderStats(data);
    renderTape(data);
    renderEvents(data);

    const phaseLabel = $("#phase-label");
    const phaseLed = $("#phase-led");
    const phase = data.clock.open ? "OPEN" : (data.clock.phase || "CLOSED");
    if (phaseLabel) phaseLabel.textContent = phase;
    if (phaseLed) phaseLed.className = "led " + (data.clock.open ? "live" : "closed");

    const dateLabel = $("#clock-date");
    if (dateLabel) dateLabel.textContent = data.clock.date_label;

    const status = $("#sentinel-status");
    if (status) {
      status.className = "status" + (data.sentinel.running ? " live" : "");
      status.innerHTML = `<i></i> ${data.sentinel.running ? "armed" : "idle"}`;
    }
    const toggle = $("#toggle-sentinel");
    if (toggle && !busy) {
      toggle.textContent = data.sentinel.running ? "Disarm sentinel" : "Arm sentinel";
    }
    const nextRun = $("#next-run");
    if (nextRun) {
      nextRun.textContent = data.sentinel.running
        ? `Next sweep ${nextSweepLabel(data)} · every ${data.sentinel.interval_minutes} min`
        : "Sentinel idle — no autonomous sweeps";
    }
    const scanTime = $("#scan-time");
    if (scanTime) {
      scanTime.textContent = data.sentinel.last_scan_label
        ? `Last sweep ${data.sentinel.last_scan_label} IST · ${data.market.scanned} instruments`
        : "No sweep yet";
    }

    if (currentView === "dashboard") {
      const sub = $("#view-sub");
      if (sub) {
        sub.textContent = data.market.scanned
          ? `${data.market.actionable} actionable of ${data.market.scanned} instruments · ${data.clock.open ? "market open" : "market closed"}`
          : `Awaiting first sweep · ${data.clock.open ? "market open" : "market closed"}`;
      }
    }
  }

  /* ------------------------------------------------------------ chart */
  async function loadSymbols() {
    try {
      const data = await api("/api/symbols");
      return data.symbols || [];
    } catch (error) {
      toast(error.message, "error");
      return [];
    }
  }

  async function loadChart(symbol) {
    if (!symbol) return;
    const $error = $("#chart-error");
    try {
      $error.textContent = "Loading…";
      const data = await api("/api/chart?symbol=" + encodeURIComponent(symbol));
      chartState = data;
      renderChart(data);
      $("#symbol-hint").textContent = data.name + " · " + data.timeframe + " chart";
    } catch (error) {
      $error.textContent = error.message;
      chartState = null;
    }
  }

  function renderChart(data) {
    const host = $("#chart");
    if (!host) return;
    if (!data.bars || data.bars.length === 0) {
      host.innerHTML = `<div class="empty">No bars yet. Press <b>Scan now</b>.</div>`;
      return;
    }
    host.innerHTML = "";

    // price line
    const maxPrice = Math.max(...data.bars.map(b => b.close));
    const minPrice = Math.min(...data.bars.map(b => b.low));
    const range = maxPrice - minPrice || 1;
    const width = 240, height = 160;
    let body = "";
    data.bars.forEach((bar, i) => {
      const x = (i / (data.bars.length - 1 || 1)) * width;
      const y = height - ((bar.close - minPrice) / range) * (height - 8) - 4;
      body += `<rect x="${x.toFixed(1)}" y="0" width="1" height="0" style="fill:${bar.close >= (data.bars[i-1]?.close ?? bar.close) ? '#16a34a' : '#dc2626'}" />`;
    });
    host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" style="width:100%;height:100%;display:block">
      <line x1="0" y1="0" x2="0" y2="1" stroke="#e5e7eb"/>
      ${body}
      <text x="${width - 4}" y="${height - 4}" text-anchor="end" font-size="10" fill="#6b7280">${num(data.bars[data.bars.length-1]?.close ?? 0)}</text>
    </svg>`;
    host.appendChild(document.createElement("div")).className = "muted small";
  }

  function watchSymbol(input, select) {
    const onKey = (e) => {
      if (e.key === "Enter" || e.blur) {
        const val = input.value.trim().toUpperCase();
        if (val) loadChart(val);
      }
    };
    input.addEventListener("keydown", onKey);
    input.addEventListener("blur", onKey);
    function refresh() {
      const list = $$("#chart-list option");
      const match = list.find(o => o.value.toUpperCase() === input.value.trim().toUpperCase());
      input.value = match ? match.value : input.value;
      if (input.value.trim()) loadChart(input.value.trim().toUpperCase());
    }
    setInterval(refresh, 1500);
  }

  /* ------------------------------------------------------------ actions */
  async function refresh() {
    try {
      renderAll(await api("/api/state"));
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function toggleSentinel() {
    const button = $("#toggle-sentinel");
    const action = state && state.sentinel.running ? "stop" : "start";
    const restore = setBusy(button, action === "start" ? "Arming…" : "Stopping…");
    busy = true;
    try {
      const data = await api("/api/sentinel", { method: "POST", body: { action } });
      renderAll(data.state);
      toast(action === "start" ? "Sentinel armed — sweeps run only during market hours." : "Sentinel stopped.");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      busy = false;
      restore();
      if (state) renderAll(state);
    }
  }

  async function scanNow() {
    const button = $("#scan-now");
    const restore = setBusy(button, "Scanning…");
    try {
      const data = await api("/api/scan", { method: "POST", body: { force: true } });
      renderAll(data.state);
      toast(`Sweep complete · ${data.scanned} instruments · ${data.actionable} actionable`);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  function openReport(symbol) {
    window.open(`/api/report?symbol=${encodeURIComponent(symbol)}`, "_blank", "noopener");
  }

  /* ----------------------------------------------------------- settings */
  function fieldControl(field) {
    const hint = field.help ? `<span class="hint">${esc(field.help)}</span>` : "";
    if (field.kind === "bool") {
      const checked = String(field.value).toLowerCase() === "true" ? "checked" : "";
      return `<label class="field field-check">
        <input type="checkbox" data-key="${esc(field.key)}" data-kind="bool" ${checked} />
        <span>${esc(field.label)}</span>${hint}
      </label>`;
    }
    const inputType = field.kind === "secret" ? "password"
      : (field.kind === "int" || field.kind === "float") ? "number" : "text";
    const value = field.is_secret ? "" : esc(field.value);
    const placeholder = field.is_secret
      ? (field.is_set ? `${field.preview} — saved, type to replace` : esc(field.placeholder))
      : esc(field.placeholder);
    const bounds = [
      field.minimum !== null && field.minimum !== undefined ? `min="${field.minimum}"` : "",
      field.maximum !== null && field.maximum !== undefined ? `max="${field.maximum}"` : "",
      field.kind === "int" ? 'step="1"' : field.kind === "float" ? 'step="0.1"' : "",
    ].filter(Boolean).join(" ");
    return `<label class="field">
      <span>${esc(field.label)}</span>
      <input type="${inputType}" data-key="${esc(field.key)}" data-kind="${esc(field.kind)}"
             value="${value}" placeholder="${placeholder}" ${bounds}
             autocomplete="off" spellcheck="false" />
      ${hint}
    </label>`;
  }

  function renderSettings(snapshot) {
    const form = $("#settings-form");
    if (!form) return;
    const path = $("#env-path");
    if (path) path.textContent = snapshot.env_path;
    form.innerHTML = snapshot.groups.map((group) => {
      const fields = snapshot.fields.filter((field) => field.group === group.id);
      const test = TESTABLE[group.id]
        ? `<button class="btn small" type="button" data-test="${group.id}">Send test</button>`
        : "";
      return `<section class="group">
        <div class="group-head">
          <div><h3>${esc(group.title)}</h3><p class="muted small">${esc(group.description)}</p></div>
          ${test}
        </div>
        <div class="fields">${fields.map(fieldControl).join("")}</div>
      </section>`;
    }).join("");
  }

  async function loadSettings() {
    try {
      settings = await api("/api/settings");
      renderSettings(settings);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function collectValues(root) {
    const values = {};
    $$("[data-key]", root).forEach((input) => {
      const key = input.dataset.key;
      const kind = input.dataset.kind;
      input.classList.remove("input-error");
      if (kind === "secret") {
        if (input.value.trim()) values[key] = input.value.trim();
        return;
      }
      if (kind === "bool") {
        values[key] = input.checked;
        return;
      }
      values[key] = input.value;
    });
    return values;
  }

  async function saveSettings() {
    const form = $("#settings-form");
    if (!form) return;
    const values = collectValues(form);

    const button = $("#save-settings");
    const restore = setBusy(button, "Saving…");
    try {
      const data = await api("/api/settings", { method: "POST", body: { values } });
      settings = data.settings;
      renderSettings(settings);
      renderAll(data.state);
      checkUpdate();
      toast("Configuration saved and applied.");
    } catch (error) {
      toast(error.message, "error");
      if (error.fields) {
        Object.entries(error.fields).forEach(([key, message]) => {
          const input = $(`[data-key="${key}"]`, form);
          if (input) {
            input.classList.add("input-error");
            input.title = message;
          }
        });
      }
    } finally {
      restore();
    }
  }

  async function runChannelTest(channel) {
    const button = $(`[data-test="${channel}"]`);
    const restore = setBusy(button, "Testing…");
    try {
      const data = await api("/api/test", { method: "POST", body: { channel } });
      toast(data.detail || (data.ok ? "Test passed." : "Test failed."), data.ok ? "info" : "error");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  /* ---------------------------------------------------------- onboarding */
  const SETUP_GROUPS = ["telegram", "email", "groq"];

  function renderSetup() {
    const body = $("#setup-body");
    if (!body) return;
    if (!settings) {
      body.innerHTML = `<p class="muted small">Could not load the form. Open Configuration instead.</p>`;
      return;
    }
    const fields = settings.fields.filter((field) => SETUP_GROUPS.includes(field.group));
    body.innerHTML = fields.map(fieldControl).join("");
  }

  async function openSetup(force = false) {
    const modal = $("#setup");
    if (!modal) return;
    if (force) sessionStorage.removeItem("tradevmx.setupSkipped");
    if (!settings) await loadSettings();
    renderSetup();
    modal.classList.remove("hidden");
    const first = $("#setup-body input");
    if (first) first.focus();
  }

  function closeSetup() {
    const modal = $("#setup");
    if (modal) modal.classList.add("hidden");
  }

  async function saveSetup() {
    const modal = $("#setup");
    if (!modal) return;
    const values = collectValues(modal);
    values.SETUP_DONE = true;
    const button = $("#setup-save");
    const restore = setBusy(button, "Saving…");
    try {
      const data = await api("/api/settings", { method: "POST", body: { values } });
      settings = data.settings;
      renderSettings(settings);
      renderAll(data.state);
      closeSetup();
      toast("Setup saved — the desk is armed and alerting.");
    } catch (error) {
      toast(error.message, "error");
      if (error.fields) {
        Object.entries(error.fields).forEach(([key, message]) => {
          const input = $(`[data-key="${key}"]`, modal);
          if (input) {
            input.classList.add("input-error");
            input.title = message;
          }
        });
        toast("Some fields need attention — hover the highlighted input.", "error");
      }
    } finally {
      restore();
    }
  }

  function skipSetup() {
    sessionStorage.setItem("tradevmx.setupSkipped", "1");
    closeSetup();
  }

  /* -------------------------------------------------------------- updates */
  function renderUpdate(info) {
    const host = $("#update-banner");
    if (!host) return;
    if (!info || !info.available) {
      host.classList.add("hidden");
      return;
    }
    host.classList.remove("hidden");
    host.innerHTML = `
      <span class="banner-dot" aria-hidden="true"></span>
      <span><b>v${esc(info.latest)}</b> is available — this build is v${esc(info.current)}.</span>
      <a class="btn small" href="${esc(info.url)}" target="_blank" rel="noopener noreferrer">Download</a>`;
  }

  async function checkUpdate() {
    try {
      renderUpdate(await api("/api/update"));
    } catch (error) {
      renderUpdate(null);
    }
  }

  async function boot() {
    await Promise.all([refresh(), loadSettings()]);
    if (
      state && state.setup && state.setup.needs_setup &&
      sessionStorage.getItem("tradevmx.setupSkipped") !== "1"
    ) {
      openSetup();
    }
    checkUpdate();
  }

  /* ---------------------------------------------------------------- log */
  async function loadLog() {
    const output = $("#log-output");
    if (!output) return;
    try {
      const response = await fetch("/api/logs?lines=300", { cache: "no-store" });
      output.textContent = await response.text();
      output.scrollTop = output.scrollHeight;
    } catch (error) {
      output.textContent = `Could not read the log: ${error.message}`;
    }
  }

  /* --------------------------------------------------------------- views */
  function setView(view) {
    currentView = view;
    $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
    $$(".view").forEach((section) => section.classList.toggle("hidden", section.id !== `view-${view}`));
    const meta = VIEW_META[view] || VIEW_META.dashboard;
    const title = $("#view-title");
    const sub = $("#view-sub");
    if (title) title.textContent = meta[0];
    if (sub) sub.textContent = meta[1];
    if (view === "settings" && !settings) loadSettings();
    if (view === "activity") loadLog();
    if (view === "dashboard" && state) renderAll(state);
  }

  /* --------------------------------------------------------------- wiring */
  function wire() {
    $$(".nav-item").forEach((item) => {
      item.addEventListener("click", () => setView(item.dataset.view));
    });
    $("#toggle-sentinel")?.addEventListener("click", toggleSentinel);
    $("#scan-now")?.addEventListener("click", scanNow);
    $("#refresh-state")?.addEventListener("click", refresh);
    $("#symbol-search")?.addEventListener("input", () => {
      const val = $("#symbol-search").value.trim().toUpperCase();
      if (val) loadChart(val);
    });
    $("#timeframe-select")?.addEventListener("change", () => {
      if (chartState) {
        const tf = $("#timeframe-select").value;
        chartState.timeframe = tf;
        loadChart(chartState.symbol);
      }
    });
    $("#save-settings")?.addEventListener("click", saveSettings);
    $("#refresh-log")?.addEventListener("click", loadLog);
    $("#setup-save")?.addEventListener("click", saveSetup);
    $("#setup-skip")?.addEventListener("click", skipSetup);
    $("#start-setup")?.addEventListener("click", () => openSetup(true));

    $("#tape")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-report]");
      if (button) openReport(button.dataset.report);
    });

    $("#settings-form")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-test]");
      if (button) runChannelTest(button.dataset.test);
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });

    window.setInterval(renderClock, 1000);
    window.setInterval(() => {
      if (!document.hidden) refresh();
    }, 6000);
  }

  wire();
  boot();
})();
