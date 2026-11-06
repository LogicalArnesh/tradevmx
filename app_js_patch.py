p = 'web/static/app.js'
s = open(p, encoding='utf-8').read()

# 1) chart state + render
s = s.replace(
    "  let state = null;\n  let settings = null;",
    "  let state = null;\n  let settings = null;\n  let chartState = null;\n  let settings = null;",
)

s = s.replace(
    "  /* ------------------------------------------------------------ actions */",
    """  /* ------------------------------------------------------------ chart */\n  async function loadSymbols() {\n    try {\n      const data = await api(\"/api/symbols\");\n      return data.symbols || [];\n    } catch (error) {\n      toast(error.message, \"error\");\n      return [];\n    }\n  }\n\n  async function loadChart(symbol) {\n    if (!symbol) return;\n    const $error = $(\"#chart-error\");\n    try {\n      $error.textContent = \"Loading…\";\n      const data = await api(\"/api/chart?symbol=\" + encodeURIComponent(symbol));\n      chartState = data;\n      renderChart(data);\n      $("#symbol-hint").textContent = data.name + \" · \" + data.timeframe + \" chart\";\n    } catch (error) {\n      $error.textContent = error.message;\n      chartState = null;\n    }\n  }\n\n  function renderChart(data) {\n    const host = $(\"#chart\");\n    if (!host) return;\n    if (!data.bars || data.bars.length === 0) {\n      host.innerHTML = `<div class=\"empty\">No bars yet. Press <b>Scan now</b>.</div>`;\n      return;\n    }\n    host.innerHTML = \"\";\n\n    // price line\n    const maxPrice = Math.max(...data.bars.map(b => b.close));\n    const minPrice = Math.min(...data.bars.map(b => b.low));\n    const range = maxPrice - minPrice || 1;\n    const width = 240, height = 160;\n    let body = \"\";\n    data.bars.forEach((bar, i) => {\n      const x = (i / (data.bars.length - 1 || 1)) * width;\n      const y = height - ((bar.close - minPrice) / range) * (height - 8) - 4;\n      body += `<rect x=\"${x.toFixed(1)}\" y=\"0\" width=\"1\" height=\"0\" style=\"fill:${bar.close >= (data.bars[i-1]?.close ?? bar.close) ? '#16a34a' : '#dc2626'}\" />`;\n    });\n    host.innerHTML = `<svg viewBox=\"0 0 ${width} ${height}\" style=\"width:100%;height:100%;display:block\">\n      <line x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\" stroke=\"#e5e7eb\"/>\n      ${body}\n      <text x=\"${width - 4}\" y=\"${height - 4}\" text-anchor=\"end\" font-size=\"10\" fill=\"#6b7280\">${num(data.bars[data.bars.length-1]?.close ?? 0)}</text>\n    </svg>`;\n    host.appendChild(document.createElement(\"div\")).className = \"muted small\";\n  }\n\n  function watchSymbol(input, select) {\n    const onKey = (e) => {\n      if (e.key === \"Enter\" || e.blur) {\n        const val = input.value.trim().toUpperCase();\n        if (val) loadChart(val);\n      }\n    };\n    input.addEventListener(\"keydown\", onKey);\n    input.addEventListener(\"blur\", onKey);\n    function refresh() {\n      const list = $$(\"#chart-list option\");\n      const match = list.find(o => o.value.toUpperCase() === input.value.trim().toUpperCase());\n      input.value = match ? match.value : input.value;\n      if (input.value.trim()) loadChart(input.value.trim().toUpperCase());\n    }\n    setInterval(refresh, 1500);\n  }\n\n  /* ------------------------------------------------------------ actions */""",
)

# 2) wire chart controls
s = s.replace(
    "    $(\"#refresh-state\")?.addEventListener(\"click\", refresh);",
    """    $(\"#refresh-state\")?.addEventListener(\"click\", refresh);
    $(\"#symbol-search\")?.addEventListener(\"input\", () => {
      const val = $(\"#symbol-search\").value.trim().toUpperCase();
      if (val) loadChart(val);
    });
    $(\"#timeframe-select\")?.addEventListener(\"change\", () => {
      if (chartState) {
        const tf = $(\"#timeframe-select\").value;
        chartState.timeframe = tf;
        loadChart(chartState.symbol);
      }
    });""",
)

open(p, 'w', encoding='utf-8').write(s)
print('patched app.js with chart rendering')
