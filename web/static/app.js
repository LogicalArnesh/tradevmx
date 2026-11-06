/* TradeVMx console client — no framework, no build step. */
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
    return (parsed >= 0 ? "+" : "") + parsed.toFixed(digits) + "%";
  };
  const money = (value) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    return "₹" + parsed.toLocaleString("en-IN", { maximumFractionDigits: 0 });
  };
  const hhmm = (seconds) => {
    try {
      return new Date(seconds * 1000).toLocaleTimeString("en-GB", {
        timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
      });
    } catch (error) {
      return "—";
    }
  };

  const VIEW_META = {
    studio: ["Studio", "Search any NSE stock to open its desk."],
    assistant: ["AI assistant", "Ask the desk, then export a branded brief."],
    settings: ["Configuration", "Keys, watchlist, reports and risk thresholds."],
    activity: ["Activity", "Everything the sentinel has done recently."],
  };
  const TESTABLE = { telegram: "Telegram", email: "Email", groq: "Groq" };

  let state = null;
  let settings = null;
  let chart = null;          // { symbol, name, timeframe, bars }
  let visibleBars = 120;
  let allBars = [];
  let levels = { supports: [], resistances: [] };
  let book = null;
  let autostartOn = false;
  let currentView = "studio";
  let symbolList = [];
  let autocomplete = null;
  let autocompleteIndex = -1;
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
      const error = new Error((payload && payload.error) || "Request failed (" + response.status + ")");
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
    window.setTimeout(() => node.remove(), 5200);
  }

  function setBusy(button, label) {
    if (!button) return () => {};
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label;
    return () => {
      button.disabled = false;
      button.textContent = original;
    };
  }

  /* --------------------------------------------------------------- clock */
  function renderClock() {
    const time = $("#clock-time");
    const date = $("#clock-date");
    const now = new Date();
    if (time) {
      time.textContent = now.toLocaleTimeString("en-GB", {
        timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      });
    }
    if (date) {
      date.textContent = now.toLocaleDateString("en-GB", {
        timeZone: "Asia/Kolkata", weekday: "short", day: "2-digit", month: "short", year: "numeric",
      });
    }
  }

  /* --------------------------------------------------------------- state */
  function renderReadiness(data) {
    const host = $("#readiness");
    if (!host) return;
    const r = (data && data.readiness) || {};
    const chips = [
      ["Telegram", r.telegram],
      ["Email", r.email],
      ["Groq", r.groq],
    ];
    host.innerHTML = chips
      .map(([label, on]) => '<span class="chip ' + (on ? "on" : "off") + '"><i></i>' + esc(label) + "</span>")
      .join("");
  }

  function renderStats() {
    const host = $("#stats");
    if (!host) return;
    if (!state) {
      host.innerHTML = '<div class="empty">Waiting for the first sweep.</div>';
      return;
    }
    const rows = (state.market && state.market.rows) || [];
    const above = rows.filter((r) => Number(r.cmp) > Number(r.vwap)).length;
    const bullish = rows.filter((r) => String(r.bias).toUpperCase() === "BULLISH").length;
    const bearish = rows.filter((r) => String(r.bias).toUpperCase() === "BEARISH").length;
    const actionable = (state.market && state.market.actionable) || 0;
    const scanned = (state.market && state.market.scanned) || 0;
    const errors = Object.keys((state.market && state.market.errors) || {}).length;
    const items = [
      ["Instruments", String(scanned), "in the sweep"],
      ["Actionable", String(actionable), "above severity floor"],
      ["Above VWAP", above + "/" + rows.length, "trend confirmation"],
      ["Breadth", bullish + " up", bearish + " down"],
      ["Sweep", state.sentinel && state.sentinel.running ? "armed" : "idle",
        state.sentinel && state.sentinel.interval_minutes ? "every " + state.sentinel.interval_minutes + "m" : "manual"],
      ["Errors", String(errors), errors ? "see activity" : "clean"],
    ];
    host.innerHTML = items
      .map(([label, value, note]) =>
        '<div class="stat"><span class="stat-label">' + esc(label) + "</span>" +
        '<span class="stat-value">' + esc(value) + "</span>" +
        '<span class="stat-note">' + esc(note) + "</span></div>")
      .join("");
  }

  function renderTape() {
    const host = $("#tape");
    if (!host) return;
    const rows = (state && state.market && state.market.rows) || [];
    const stamp = $("#scan-time");
    if (stamp) {
      stamp.textContent = state && state.sentinel && state.sentinel.last_scan_label
        ? "last sweep " + state.sentinel.last_scan_label + " IST" : "";
    }
    if (!rows.length) {
      host.innerHTML = '<div class="empty">No sweep yet. Press <b>Scan now</b>, or arm the desk.</div>';
      return;
    }
    host.innerHTML = rows.map((row) => {
      const signals = (row.signals || []).slice(0, 3).map((signal) =>
        '<span class="sig ' + esc(signal.direction) + '">' + esc(signal.label) +
        "<em>" + esc(signal.timeframe) + " · " + esc(signal.severity) + "/5</em></span>").join("");
      const verdict = row.verdict
        ? '<span class="verdict">' + esc(row.verdict) +
          (row.risk_reward ? " · 1:" + Number(row.risk_reward).toFixed(2) : "") + "</span>"
        : "";
      const biasClass = String(row.bias).toUpperCase() === "BULLISH" ? "bull"
        : String(row.bias).toUpperCase() === "BEARISH" ? "bear" : "flat";
      return '<article class="row" data-symbol="' + esc(row.symbol) + '">' +
        '<div class="row-id"><strong>' + esc(row.name) + "</strong>" +
        '<span class="muted mono">' + esc(row.symbol) + (row.stale ? " · stale" : "") + "</span></div>" +
        '<div class="row-price"><span class="mono price">' + num(row.cmp) + "</span>" +
        '<span class="mono ' + (Number(row.change_pct) >= 0 ? "up" : "down") + '">' + pct(row.change_pct) + "</span></div>" +
        '<div class="row-bias"><span class="bias ' + biasClass + '">' + esc(row.bias) + "</span>" +
        '<span class="muted small">' + esc(row.structure) + " · VWAP " + pct(row.vwap_distance_pct) + "</span></div>" +
        '<div class="row-signals">' + (signals || '<span class="sig muted">no setup</span>') + "</div>" +
        '<div class="row-actions">' + verdict +
        '<button class="btn small" type="button" data-report="' + esc(row.name) + '">PDF</button></div>' +
        "</article>";
    }).join("");
  }

  function renderAll(data) {
    state = data;
    renderReadiness(data);
    renderStats();
    renderTape();
    const phaseLabel = $("#phase-label");
    const phaseLed = $("#phase-led");
    if (phaseLabel) phaseLabel.textContent = data.clock.open ? "OPEN" : (data.clock.phase || "CLOSED");
    if (phaseLed) phaseLed.className = "led " + (data.clock.open ? "live" : "closed");
    const toggle = $("#toggle-sentinel");
    if (toggle && data.sentinel) {
      toggle.textContent = data.sentinel.running ? "Stop desk" : "Arm desk";
    }
  }

  async function refresh() {
    try {
      renderAll(await api("/api/state"));
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function toggleSentinel() {
    const button = $("#toggle-sentinel");
    const action = state && state.sentinel && state.sentinel.running ? "stop" : "start";
    const restore = setBusy(button, action === "start" ? "Arming…" : "Stopping…");
    try {
      const data = await api("/api/sentinel", { method: "POST", body: { action } });
      renderAll(data.state);
      toast(action === "start" ? "Desk armed — sweeps run during market hours." : "Desk stopped.");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  async function scanNow() {
    const button = $("#scan-now");
    const restore = setBusy(button, "Scanning…");
    try {
      const data = await api("/api/scan", { method: "POST", body: { force: true } });
      renderAll(data.state);
      toast("Sweep complete · " + data.scanned + " instruments · " + data.actionable + " actionable");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  /* -------------------------------------------------------------- search */
  async function loadSymbols() {
    try {
      const data = await api("/api/search?limit=40");
      symbolList = data.matches || [];
    } catch (error) {
      symbolList = [];
    }
    return symbolList;
  }

  function closeAutocomplete() {
    if (autocomplete) {
      autocomplete.remove();
      autocomplete = null;
      autocompleteIndex = -1;
    }
  }

  function renderAutocomplete(input, matches) {
    closeAutocomplete();
    if (!matches.length) return;
    autocomplete = document.createElement("div");
    autocomplete.className = "autocomplete-wrap";
    autocomplete.setAttribute("role", "listbox");
    input.parentNode.appendChild(autocomplete);
    autocompleteIndex = -1;
    autocomplete.innerHTML = matches.map((entry, index) =>
      '<button type="button" class="autocomplete-item" data-index="' + index + '">' +
      '<span class="ac-symbol">' + esc(entry.symbol) + "</span>" +
      '<span class="ac-name">' + esc(entry.name) + "</span>" +
      '<span class="ac-kind">' + esc(entry.kind) + "</span></button>").join("");
    $$(".autocomplete-item", autocomplete).forEach((node) => {
      node.addEventListener("mousedown", (event) => {
        event.preventDefault();
        const entry = matches[Number(node.dataset.index)];
        if (entry) {
          input.value = entry.symbol;
          closeAutocomplete();
          openSymbol(entry.symbol, entry.name);
        }
      });
    });
  }

  async function onSearchInput() {
    const input = $("#symbol-search");
    if (!input) return;
    const query = input.value.trim();
    let matches;
    try {
      const data = await api("/api/search?q=" + encodeURIComponent(query) + "&limit=12");
      matches = data.matches || [];
    } catch (error) {
      matches = symbolList.filter((entry) =>
        entry.symbol.toUpperCase().includes(query.toUpperCase()) ||
        entry.name.toUpperCase().includes(query.toUpperCase())).slice(0, 12);
    }
    renderAutocomplete(input, matches);
  }

  /* --------------------------------------------------------------- chart */
  async function openSymbol(symbol, name) {
    if (!symbol) return;
    const timeframeSelect = $("#timeframe-select");
    const timeframe = timeframeSelect ? timeframeSelect.value : "15m";
    const errorHost = $("#chart-error");
    if (errorHost) errorHost.textContent = "Loading…";
    closeAutocomplete();
    try {
      const history = await api("/api/history?symbol=" + encodeURIComponent(symbol) +
        "&timeframe=" + encodeURIComponent(timeframe) + "&limit=220");
      allBars = history.bars || [];
      chart = {
        symbol: history.symbol || symbol,
        name: history.name || name || symbol,
        timeframe: history.timeframe || timeframe,
        source: history.source,
      };
      const hint = $("#symbol-hint");
      if (hint) {
        hint.textContent = chart.name + " · " + chart.timeframe +
          (allBars.length ? " · " + allBars.length + " bars" : " · no history");
      }
      if (errorHost) {
        errorHost.textContent = history.error && !allBars.length ? history.error : "";
      }
      try {
        const live = await api("/api/chart?symbol=" + encodeURIComponent(symbol) +
          "&timeframe=" + encodeURIComponent(timeframe));
        levels = live.levels || levels;
        if (live.current && allBars.length) {
          const last = allBars[allBars.length - 1];
          last.close = live.current;
          last.high = Math.max(last.high, live.current);
          last.low = Math.min(last.low, live.current);
        }
      } catch (error) {
        /* levels are optional: the chart still draws from history */
      }
      drawChart();
      renderExpected();
      renderLevels();
      const tradeSymbol = $("#trade-symbol");
      if (tradeSymbol && !tradeSymbol.value) tradeSymbol.value = chart.symbol;
    } catch (error) {
      if (errorHost) errorHost.textContent = error.message;
      allBars = [];
      drawChart();
    }
  }

  function drawChart() {
    const host = $("#chart");
    if (!host) return;
    if (!allBars.length) {
      host.innerHTML = '<div class="empty">No bars yet. Pick a symbol, or press <b>Scan now</b>.</div>';
      return;
    }
    const bars = allBars.slice(-Math.max(20, visibleBars));
    const width = 1000;
    const height = 420;
    const padTop = 14;
    const padBottom = 70;
    const volumeTop = height - padBottom + 12;
    const high = Math.max(...bars.map((bar) => Number(bar.high) || 0));
    const low = Math.min(...bars.map((bar) => Number(bar.low) || Infinity));
    const span = high - low || 1;
    const maxVolume = Math.max(...bars.map((bar) => Number(bar.volume) || 0), 1);
    const slot = width / bars.length;
    const body = Math.max(1.4, slot * 0.62);
    const yOf = (price) => padTop + (1 - (price - low) / span) * (height - padTop - padBottom);

    let svg = "";
    const gridLines = 5;
    for (let i = 0; i <= gridLines; i += 1) {
      const price = low + (span * i) / gridLines;
      const y = yOf(price);
      svg += '<line x1="0" y1="' + y.toFixed(1) + '" x2="' + width + '" y2="' + y.toFixed(1) +
        '" stroke="rgba(148,163,184,0.14)" stroke-width="1"/>';
      svg += '<text x="' + (width - 4) + '" y="' + (y - 4).toFixed(1) +
        '" text-anchor="end" font-size="11" fill="#7c8ba1">' + num(price) + "</text>";
    }
    bars.forEach((bar, index) => {
      const x = slot * index + slot / 2;
      const open = Number(bar.open) || Number(bar.close) || 0;
      const close = Number(bar.close) || 0;
      const highPrice = Number(bar.high) || Math.max(open, close);
      const lowPrice = Number(bar.low) || Math.min(open, close);
      const up = close >= open;
      const colour = up ? "#22c58b" : "#ef5b5b";
      const top = yOf(Math.max(open, close));
      const bottom = yOf(Math.min(open, close));
      svg += '<line x1="' + x.toFixed(1) + '" y1="' + yOf(highPrice).toFixed(1) +
        '" x2="' + x.toFixed(1) + '" y2="' + yOf(lowPrice).toFixed(1) +
        '" stroke="' + colour + '" stroke-width="1"/>';
      svg += '<rect x="' + (x - body / 2).toFixed(1) + '" y="' + top.toFixed(1) +
        '" width="' + body.toFixed(1) + '" height="' + Math.max(1, bottom - top).toFixed(1) +
        '" fill="' + colour + '" opacity="0.92"/>';
      const volumeHeight = ((Number(bar.volume) || 0) / maxVolume) * 52;
      svg += '<rect x="' + (x - body / 2).toFixed(1) + '" y="' + (volumeTop + 52 - volumeHeight).toFixed(1) +
        '" width="' + body.toFixed(1) + '" height="' + volumeHeight.toFixed(1) +
        '" fill="' + colour + '" opacity="0.35"/>';
    });
    (levels.supports || []).forEach((level) => {
      const price = Number(level && typeof level === "object" ? level.price : level);
      if (!Number.isFinite(price) || price < low || price > high) return;
      const y = yOf(price);
      svg += '<line x1="0" y1="' + y.toFixed(1) + '" x2="' + width + '" y2="' + y.toFixed(1) +
        '" stroke="#22c58b" stroke-width="1" stroke-dasharray="5 4" opacity="0.55"/>';
    });
    (levels.resistances || []).forEach((level) => {
      const price = Number(level && typeof level === "object" ? level.price : level);
      if (!Number.isFinite(price) || price < low || price > high) return;
      const y = yOf(price);
      svg += '<line x1="0" y1="' + y.toFixed(1) + '" x2="' + width + '" y2="' + y.toFixed(1) +
        '" stroke="#ef5b5b" stroke-width="1" stroke-dasharray="5 4" opacity="0.55"/>';
    });
    svg += '<line class="crosshair" x1="0" y1="0" x2="0" y2="' + height + '" stroke="#8fb4ff" stroke-width="1" opacity="0"/>';

    host.innerHTML = '<svg id="chart-svg" viewBox="0 0 ' + width + " " + height +
      '" preserveAspectRatio="none" style="width:100%;height:100%;display:block">' + svg + "</svg>";
    attachCrosshair(bars, width, height, slot);
  }

  function attachCrosshair(bars, width, height, slot) {
    const svg = $("#chart-svg");
    const readout = $("#chart-readout");
    if (!svg || !readout) return;
    const line = $(".crosshair", svg);
    svg.addEventListener("mousemove", (event) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const x = ((event.clientX - rect.left) / rect.width) * width;
      const index = Math.max(0, Math.min(bars.length - 1, Math.floor(x / slot)));
      const bar = bars[index];
      if (!bar) return;
      if (line) {
        const cx = (slot * index + slot / 2).toFixed(1);
        line.setAttribute("x1", cx);
        line.setAttribute("x2", cx);
        line.setAttribute("opacity", "0.45");
      }
      readout.textContent = hhmm(bar.time) + "  O " + num(bar.open) + "  H " + num(bar.high) +
        "  L " + num(bar.low) + "  C " + num(bar.close) + "  V " +
        Number(bar.volume || 0).toLocaleString("en-IN");
    });
    svg.addEventListener("mouseleave", () => {
      if (line) line.setAttribute("opacity", "0");
      readout.textContent = "Move the cursor over the chart";
    });
  }

  function renderLevels() {
    const host = $("#chart-levels");
    if (!host) return;
    const parts = [];
    (levels.supports || []).forEach((level) => {
      const price = Number(level && typeof level === "object" ? level.price : level);
      if (Number.isFinite(price)) parts.push('<span class="chip bg-support">' + num(price) + " S</span>");
    });
    (levels.resistances || []).forEach((level) => {
      const price = Number(level && typeof level === "object" ? level.price : level);
      if (Number.isFinite(price)) parts.push('<span class="chip bg-resistance">' + num(price) + " R</span>");
    });
    host.innerHTML = parts.length ? "<span class=\"muted small\">Levels</span>" + parts.join("") : "";
  }

  /* ------------------------------------------------------------ expected */
  function renderExpected() {
    const host = $("#expected");
    if (!host) return;
    if (!allBars.length) {
      host.innerHTML = '<div class="empty">Load a chart to project the session.</div>';
      return;
    }
    const bars = allBars.slice(-Math.max(20, Math.min(visibleBars, allBars.length)));
    const ranges = bars.map((bar) => Math.abs(Number(bar.high) - Number(bar.low))).filter(Number.isFinite);
    const atr = ranges.length ? ranges.reduce((a, b) => a + b, 0) / ranges.length : 0;
    const last = bars[bars.length - 1];
    const close = Number(last.close) || 0;
    const high = Math.max(...bars.map((bar) => Number(bar.high) || close));
    const low = Math.min(...bars.map((bar) => Number(bar.low) || close));
    const pivot = (high + low + close) / 3;
    const r1 = 2 * pivot - low;
    const s1 = 2 * pivot - high;
    const upper = close + atr * 0.9;
    const lower = close - atr * 0.9;
    const drift = bars.length > 1
      ? (close - (Number(bars[0].close) || close)) / (Number(bars[0].close) || 1) * 100 : 0;

    // Volatility by hour -> the best window to transact.
    const buckets = {};
    bars.forEach((bar) => {
      const hour = new Date(bar.time * 1000).getUTCHours(); // storage is UTC epochs
      const span = Math.abs(Number(bar.high) - Number(bar.low));
      if (!buckets[hour]) buckets[hour] = { sum: 0, count: 0 };
      buckets[hour].sum += span;
      buckets[hour].count += 1;
    });
    let bestHour = null;
    let bestAvg = -1;
    Object.keys(buckets).forEach((hour) => {
      const avg = buckets[hour].sum / Math.max(1, buckets[hour].count);
      if (avg > bestAvg) {
        bestAvg = avg;
        bestHour = Number(hour);
      }
    });
    const istHour = bestHour === null ? null : (bestHour + 5 + 0.5) % 24;
    const window = istHour === null ? "—"
      : String(Math.floor(istHour)).padStart(2, "0") + ":00–" +
        String(Math.floor(istHour) + 1).padStart(2, "0") + ":00 IST";

    const rows = [
      ["Projected range", num(lower) + " – " + num(upper), "0.9 × ATR either side"],
      ["Pivot", num(pivot), "classic floor pivot"],
      ["First resistance", num(r1), "R1"],
      ["First support", num(s1), "S1"],
      ["Average bar range", num(atr), "ATR(" + bars.length + ")"],
      ["Drift in window", pct(drift), "close vs window open"],
      ["Best window", window, "highest average range"],
    ];
    host.innerHTML = rows.map(([label, value, note]) =>
      '<div class="expect-row"><span class="stat-label">' + esc(label) + "</span>" +
      '<span class="stat-value">' + esc(value) + "</span>" +
      '<span class="stat-note">' + esc(note) + "</span></div>").join("");
  }

  /* ---------------------------------------------------------------- book */
  function renderBook() {
    const host = $("#book");
    if (!host) return;
    if (!book) {
      host.innerHTML = '<div class="empty">Loading the paper account…</div>';
      return;
    }
    const status = $("#book-status");
    if (status) status.textContent = "cash " + money(book.cash);
    const head = '<div class="book-head">' +
      '<div><span class="stat-label">Equity</span><span class="stat-value">' + money(book.equity) + "</span></div>" +
      '<div><span class="stat-label">P&amp;L</span><span class="stat-value ' +
      (Number(book.unrealised) >= 0 ? "up" : "down") + '">' + money(book.unrealised) + "</span></div>" +
      '<div><span class="stat-label">Return</span><span class="stat-value ' +
      (Number(book.return_pct) >= 0 ? "up" : "down") + '">' + pct(book.return_pct) + "</span></div>" +
      "</div>";
    const positions = (book.positions || []).length
      ? '<table class="book-table"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th><th>P&amp;L</th></tr></thead><tbody>' +
        book.positions.map((position) =>
          "<tr><td>" + esc(position.symbol) + "</td><td>" + Number(position.qty).toLocaleString("en-IN") +
          "</td><td>" + num(position.avg) + "</td><td>" + num(position.ltp) + '</td><td class="' +
          (Number(position.pnl) >= 0 ? "up" : "down") + '">' + money(position.pnl) + "</td></tr>").join("") +
        "</tbody></table>"
      : '<div class="empty">No positions. Use the form below to simulate a fill.</div>';
    const trades = (book.trades || []).slice(0, 6).map((trade) =>
      '<div class="trade-row"><span class="mono small">' + esc((trade.at || "").slice(11, 16)) + "</span>" +
      '<span class="' + (trade.action === "BUY" ? "up" : "down") + '">' + esc(trade.action) + "</span>" +
      "<span>" + esc(trade.symbol) + "</span>" +
      '<span class="muted small">' + Number(trade.qty).toLocaleString("en-IN") + " @ " + num(trade.price) + "</span></div>").join("");
    host.innerHTML = head + positions + (trades ? '<div class="trades">' + trades + "</div>" : "");
  }

  async function loadBook() {
    try {
      book = await api("/api/portfolio");
      renderBook();
    } catch (error) {
      const host = $("#book");
      if (host) host.innerHTML = '<div class="empty">' + esc(error.message) + "</div>";
    }
  }

  async function trade(action) {
    const symbol = ($("#trade-symbol") || {}).value || (chart ? chart.symbol : "");
    const qty = ($("#trade-qty") || {}).value;
    const priceField = $("#trade-price");
    const price = priceField ? priceField.value : "";
    if (!symbol) return toast("Pick a symbol first.", "error");
    if (!qty) return toast("Enter a quantity.", "error");
    try {
      const data = await api("/api/portfolio", {
        method: "POST",
        body: {
          action,
          symbol: String(symbol).trim().toUpperCase(),
          qty: Number(qty),
          price: price ? Number(price) : null,
          name: chart && chart.name ? chart.name : symbol,
        },
      });
      book = data.book;
      renderBook();
      toast(action + " " + qty + " " + String(symbol).toUpperCase() + " filled.");
      const qtyField = $("#trade-qty");
      if (qtyField) qtyField.value = "";
      refresh();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  /* ----------------------------------------------------------- assistant */
  function pushBubble(text, who) {
    const log = $("#assistant-log");
    if (!log) return;
    const node = document.createElement("div");
    node.className = "bubble " + who;
    node.textContent = text;
    log.appendChild(node);
    log.scrollTop = log.scrollHeight;
  }

  async function ask(question) {
    const text = String(question || "").trim();
    if (!text) return;
    pushBubble(text, "me");
    const button = $("#assistant-send");
    const restore = setBusy(button, "Thinking…");
    try {
      const data = await api("/api/assistant", { method: "POST", body: { question: text } });
      pushBubble(data.answer + (data.source === "local" ? "\n\n(local digest — add a Groq key for model answers)" : ""), "bot");
      if (data.report) addReportLink(data.report);
    } catch (error) {
      pushBubble("I could not answer: " + error.message, "bot");
    } finally {
      restore();
    }
  }

  function addReportLink(report) {
    const log = $("#assistant-log");
    if (!log || !report || !report.url) return;
    const wrap = document.createElement("div");
    wrap.className = "bubble bot";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn primary small";
    button.textContent = report.label || "Download the full report (PDF)";
    button.addEventListener("click", async () => {
      const restore = setBusy(button, "Building…");
      try {
        await downloadPdf(report.url, "TradeVMx_Market_Report.pdf", () =>
          pushBubble("Full report exported — cover, charts, indicator panels, plans and sources.", "bot"));
      } catch (error) {
        toast(error.message, "error");
        pushBubble("The report could not be built: " + error.message, "bot");
      } finally {
        restore();
      }
    });
    wrap.appendChild(button);
    if (report.note) {
      const note = document.createElement("div");
      note.className = "muted small";
      note.textContent = report.note;
      wrap.appendChild(note);
    }
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  async function fullReport() {
    const button = $("#report-session");
    const restore = setBusy(button, "Building report…");
    try {
      await downloadPdf("/api/report/session", "TradeVMx_Market_Report.pdf", () =>
        pushBubble("Full report exported — one page per instrument with its chart, " +
          "indicator panel, plan and the data sources.", "bot"));
    } catch (error) {
      toast(error.message, "error");
      pushBubble("The report could not be built: " + error.message, "bot");
    } finally {
      restore();
    }
  }

  async function dailyBrief() {
    const button = $("#daily-brief");
    const restore = setBusy(button, "Building…");
    try {
      const response = await fetch("/api/report/daily", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-TradeVMx-Token": TOKEN },
        body: "{}",
      });
      if (!response.ok) {
        let detail = "brief failed (" + response.status + ")";
        try {
          const payload = await response.json();
          detail = payload.error || detail;
        } catch (error) { /* not json */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const disposition = response.headers.get("content-disposition") || "";
      const match = /filename="?([^";]+)"?/.exec(disposition);
      link.href = url;
      link.download = match ? match[1] : "TradeVMx_Daily_Brief.pdf";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 4000);
      pushBubble("Daily brief exported. Check your downloads — it is branded with the licence and copyright block.", "bot");
    } catch (error) {
      pushBubble("Could not build the brief: " + error.message, "bot");
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  async function createShortcut() {
    try {
      const data = await api("/api/shortcut", { method: "POST", body: {} });
      toast(data.ok ? data.detail : "Shortcut: " + data.detail, data.ok ? "info" : "error");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function downloadPdf(path, fallbackName, onDone) {
    const response = await fetch(path, {
      headers: { "X-TradeVMx-Token": TOKEN },
      cache: "no-store",
    });
    if (!response.ok) {
      let detail = "download failed (" + response.status + ")";
      try {
        const payload = await response.json();
        detail = payload.error || detail;
      } catch (error) { /* not json */ }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const disposition = response.headers.get("content-disposition") || "";
    const match = /filename="?([^";]+)"?/.exec(disposition);
    link.href = url;
    link.download = match ? match[1] : fallbackName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 4000);
    if (onDone) onDone();
  }

  async function digestPdf() {
    const button = $("#digest-pdf");
    const restore = setBusy(button, "Building…");
    try {
      await downloadPdf("/api/watchlist/digest", "TradeVMx_Watchlist_Digest.pdf", () =>
        pushBubble("Watchlist digest exported — predictions, levels, mini charts and sources.", "bot"));
    } catch (error) {
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  async function digestEmail() {
    const button = $("#digest-email");
    const restore = setBusy(button, "Sending…");
    try {
      const data = await api("/api/watchlist/digest", { method: "POST", body: { send: true } });
      let detail;
      if (data.ok) {
        detail = "Digest sent" + (data.email ? " by email" : "") +
          (data.telegram ? (data.email ? " and Telegram" : " to Telegram") : "") +
          " · " + (data.pdf || "PDF");
      } else {
        detail = data.reason || "Nothing was sent — add a Telegram or Gmail channel in Configuration.";
      }
      toast(detail, data.ok ? "info" : "error");
      pushBubble(detail, "bot");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  async function pollUpdate(force) {
    try {
      const data = await api("/api/update" + (force ? "?force=1" : ""));
      const banner = $("#update-banner");
      const status = $("#update-status");
      if (banner) {
        if (data && data.available) {
          banner.classList.remove("hidden");
          banner.innerHTML = '<span class="banner-dot"></span><span><b>v' + esc(data.latest) +
            "</b> is available — this build is v" + esc(data.current) + ".</span>" +
            '<a class="btn small" href="' + esc(data.url) +
            '" target="_blank" rel="noopener noreferrer">Download</a>';
        } else {
          banner.classList.add("hidden");
        }
      }
      if (status) {
        status.textContent = data && data.available
          ? "v" + data.latest + " available"
          : "up to date · v" + ((data && data.current) || "");
      }
      if (force) {
        toast(data && data.available
          ? "v" + data.latest + " is available — use the banner to download."
          : "You are on the latest version.");
      }
    } catch (error) {
      if (force) toast(error.message, "error");
    }
  }

  async function toggleAutostart() {
    const button = $("#autostart-toggle");
    if (!button) return;
    const target = !autostartOn;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Working…";
    try {
      const data = await api("/api/autostart", { method: "POST", body: { enabled: target } });
      toast(data.detail, data.ok ? "info" : "error");
      if (data.ok) autostartOn = Boolean(data.enabled);
      button.textContent = autostartOn ? "Remove from startup" : "Run at Windows startup";
    } catch (error) {
      button.textContent = original;
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  /* ------------------------------------------------------------ settings */
  function fieldControl(field) {
    const hint = field.help ? '<span class="hint">' + esc(field.help) + "</span>" : "";
    if (field.kind === "bool") {
      const checked = String(field.value).toLowerCase() === "true" ? "checked" : "";
      return '<label class="field field-check"><input type="checkbox" data-key="' + esc(field.key) +
        '" data-kind="bool" ' + checked + "/><span>" + esc(field.label) + "</span>" + hint + "</label>";
    }
    const inputType = field.kind === "secret" ? "password"
      : (field.kind === "int" || field.kind === "float") ? "number" : "text";
    const value = field.is_secret ? "" : esc(field.value);
    const placeholder = field.is_secret
      ? (field.is_set ? field.preview + " — saved, type to replace" : esc(field.placeholder))
      : esc(field.placeholder);
    const bounds = [
      field.minimum !== null && field.minimum !== undefined ? 'min="' + field.minimum + '"' : "",
      field.maximum !== null && field.maximum !== undefined ? 'max="' + field.maximum + '"' : "",
      field.kind === "int" ? 'step="1"' : field.kind === "float" ? 'step="0.1"' : "",
    ].filter(Boolean).join(" ");
    return '<label class="field"><span>' + esc(field.label) + "</span>" +
      '<input type="' + inputType + '" data-key="' + esc(field.key) + '" data-kind="' + esc(field.kind) +
      '" value="' + value + '" placeholder="' + placeholder + '" ' + bounds +
      ' autocomplete="off" spellcheck="false"/>' + hint + "</label>";
  }

  function renderSettings(snapshot) {
    const form = $("#settings-form");
    if (!form) return;
    const path = $("#env-path");
    if (path) path.textContent = snapshot.env_path;
    form.innerHTML = snapshot.groups.map((group) => {
      const fields = snapshot.fields.filter((field) => field.group === group.id);
      const test = TESTABLE[group.id]
        ? '<button class="btn small" type="button" data-test="' + group.id + '">Send test</button>' : "";
      return '<section class="group"><div class="group-head"><div><h3>' + esc(group.title) +
        '</h3><p class="muted small">' + esc(group.description) + "</p></div>" + test + "</div>" +
        '<div class="fields">' + fields.map(fieldControl).join("") + "</div></section>";
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

  async function saveSettings(extra) {
    const form = $("#settings-form");
    if (!form) return;
    const values = Object.assign(collectValues(form), extra || {});
    const button = $("#save-settings");
    const restore = setBusy(button, "Saving…");
    try {
      const data = await api("/api/settings", { method: "POST", body: { values } });
      settings = data.settings;
      renderSettings(settings);
      renderAll(data.state);
      toast("Configuration saved and applied.");
    } catch (error) {
      toast(error.message, "error");
      if (error.fields) {
        Object.keys(error.fields).forEach((key) => {
          const input = $('[data-key="' + key + '"]', form);
          if (input) {
            input.classList.add("input-error");
            input.title = error.fields[key];
          }
        });
      }
    } finally {
      restore();
    }
  }

  async function runChannelTest(channel) {
    const button = $('[data-test="' + channel + '"]');
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
      body.innerHTML = '<p class="muted small">Could not load the form. Open Configuration instead.</p>';
      return;
    }
    body.innerHTML = settings.fields.filter((field) => SETUP_GROUPS.includes(field.group))
      .map(fieldControl).join("");
  }

  async function openSetup(force) {
    const modal = $("#setup");
    if (!modal) return;
    if (!settings) await loadSettings();
    renderSetup();
    modal.classList.remove("hidden");
    const first = $("#setup-body input");
    if (first) first.focus();
    if (force) modal.dataset.forced = "1";
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
        Object.keys(error.fields).forEach((key) => {
          const input = $('[data-key="' + key + '"]', modal);
          if (input) {
            input.classList.add("input-error");
            input.title = error.fields[key];
          }
        });
      }
    } finally {
      restore();
    }
  }

  /* -------------------------------------------------------------- licence */
  async function checkLicence() {
    try {
      const data = await api("/api/license");
      const meta = $("#license-meta");
      if (meta) meta.textContent = "Licence " + data.licence + " · " + data.copyright;
      if (!data.accepted) {
        const gate = $("#license-gate");
        if (gate) gate.classList.remove("hidden");
        return false;
      }
    } catch (error) {
      /* a licence read failure must not lock the desk */
    }
    return true;
  }

  async function acceptLicence() {
    const button = $("#license-accept");
    const restore = setBusy(button, "Opening…");
    try {
      await api("/api/license", { method: "POST", body: { accepted: true } });
      const gate = $("#license-gate");
      if (gate) gate.classList.add("hidden");
      toast("Licence accepted. Welcome to TradeVMx.");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      restore();
    }
  }

  /* ---------------------------------------------------------------- views */
  function setView(view) {
    currentView = view;
    $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
    $$(".view").forEach((section) => section.classList.toggle("hidden", section.id !== "view-" + view));
    const meta = VIEW_META[view] || VIEW_META.studio;
    const title = $("#view-title");
    const sub = $("#view-sub");
    if (title) title.textContent = meta[0];
    if (sub) sub.textContent = meta[1];
    if (view === "settings" && !settings) loadSettings();
    if (view === "activity") loadLog();
  }

  async function loadLog() {
    const output = $("#log-output");
    if (!output) return;
    try {
      const response = await fetch("/api/logs?lines=300", { cache: "no-store" });
      output.textContent = await response.text();
      output.scrollTop = output.scrollHeight;
    } catch (error) {
      output.textContent = "Could not read the log: " + error.message;
    }
  }

  /* --------------------------------------------------------------- wiring */
  function wire() {
    $$(".nav-item").forEach((item) => {
      item.addEventListener("click", () => setView(item.dataset.view));
    });
    $("#toggle-sentinel")?.addEventListener("click", toggleSentinel);
    $("#scan-now")?.addEventListener("click", scanNow);
    $("#refresh-state")?.addEventListener("click", refresh);
    $("#save-settings")?.addEventListener("click", () => saveSettings());
    $("#refresh-log")?.addEventListener("click", loadLog);
    $("#setup-save")?.addEventListener("click", saveSetup);
    $("#setup-skip")?.addEventListener("click", closeSetup);
    $("#start-setup")?.addEventListener("click", () => openSetup(true));
    $("#license-accept")?.addEventListener("click", acceptLicence);
    $("#report-session")?.addEventListener("click", fullReport);
    $("#daily-brief")?.addEventListener("click", dailyBrief);
    $("#digest-pdf")?.addEventListener("click", digestPdf);
    $("#digest-email")?.addEventListener("click", digestEmail);
    $("#check-update")?.addEventListener("click", () => pollUpdate(true));
    $("#autostart-toggle")?.addEventListener("click", toggleAutostart);
    $("#create-shortcut")?.addEventListener("click", createShortcut);
    $("#trade-buy")?.addEventListener("click", () => trade("BUY"));
    $("#trade-sell")?.addEventListener("click", () => trade("SELL"));

    const search = $("#symbol-search");
    if (search) {
      let timer = null;
      search.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(onSearchInput, 140);
      });
      search.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          const items = $$(".autocomplete-item");
          if (autocompleteIndex >= 0 && items[autocompleteIndex]) {
            items[autocompleteIndex].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
            return;
          }
          const value = search.value.trim().toUpperCase();
          if (value) openSymbol(value);
        } else if (event.key === "ArrowDown") {
          event.preventDefault();
          const items = $$(".autocomplete-item");
          if (!items.length) return;
          autocompleteIndex = Math.min(autocompleteIndex + 1, items.length - 1);
          items.forEach((node, index) => node.classList.toggle("is-active", index === autocompleteIndex));
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          const items = $$(".autocomplete-item");
          if (!items.length) return;
          autocompleteIndex = Math.max(autocompleteIndex - 1, 0);
          items.forEach((node, index) => node.classList.toggle("is-active", index === autocompleteIndex));
        } else if (event.key === "Escape") {
          closeAutocomplete();
        }
      });
      search.addEventListener("focus", () => {
        if (!symbolList.length) loadSymbols().then(() => {
          if (symbolList.length) renderAutocomplete(search, symbolList.slice(0, 10));
        });
      });
      search.addEventListener("blur", () => window.setTimeout(closeAutocomplete, 160));
    }

    $("#timeframe-select")?.addEventListener("change", () => {
      if (chart) openSymbol(chart.symbol, chart.name);
    });

    const range = $("#timeline-range");
    if (range) {
      range.addEventListener("input", () => {
        visibleBars = Number(range.value) || 120;
        const label = $("#timeline-label");
        if (label) label.textContent = visibleBars + " bars";
        drawChart();
        renderExpected();
      });
    }

    $("#tape")?.addEventListener("click", (event) => {
      const reportButton = event.target.closest("[data-report]");
      if (reportButton) {
        window.open("/api/report?symbol=" + encodeURIComponent(reportButton.dataset.report), "_blank", "noopener");
        return;
      }
      const row = event.target.closest(".row");
      if (row && row.dataset.symbol) openSymbol(row.dataset.symbol);
    });

    $("#settings-form")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-test]");
      if (button) runChannelTest(button.dataset.test);
    });

    $$("[data-ask]").forEach((button) => {
      button.addEventListener("click", () => {
        setView("assistant");
        ask(button.dataset.ask);
      });
    });

    $("#assistant-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const input = $("#assistant-input");
      if (input) {
        ask(input.value);
        input.value = "";
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });
    window.setInterval(renderClock, 1000);
    window.setInterval(() => {
      if (!document.hidden) refresh();
    }, 8000);
  }

  async function boot() {
    renderClock();
    const gateOpen = !(await checkLicence());
    await Promise.all([refresh(), loadSettings(), loadBook()]);
    const versionEl = $("#app-version");
    if (versionEl && state && state.app && state.app.version) {
      versionEl.textContent = "v" + state.app.version;
    }
    if (!gateOpen && state && state.setup && state.setup.needs_setup) {
      openSetup(false);
    }
    await pollUpdate(false);
    window.setInterval(() => pollUpdate(false), 6 * 60 * 60 * 1000);
  }

  wire();
  boot();
})();
