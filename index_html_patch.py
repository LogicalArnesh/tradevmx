p = 'web/templates/index.html'
s = open(p, encoding='utf-8').read()

old_start = s.index('      <section id="view-dashboard" class="view">')
old_end = s.index('      <section id="view-settings" class="view hidden">')
old = s[old_start:old_end]

new = '''      <section id="view-dashboard" class="view">
        <div class="panel controls">
          <div class="search-box">
            <input id="symbol-search" type="text" placeholder="Search symbol (e.g. RELIANCE, ^NSEI, NIFTY)" autocomplete="off" />
            <span id="symbol-hint" class="muted small"></span>
          </div>
          <div class="search-group">
            <label for="timeframe-select">Timeframe</label>
            <select id="timeframe-select">
              <option value="1m">1m</option>
              <option value="5m">5m</option>
              <option value="15m" selected>15m</option>
              <option value="30m">30m</option>
              <option value="1h">1h</option>
            </select>
          </div>
          <div class="search-group">
            <button id="scan-now" class="btn" type="button">Scan now</button>
            <span id="sentinel-status" class="status"><i></i> idle</span>
            <span id="next-run" class="muted small">No schedule</span>
            <span class="spacer"></span>
            <button id="refresh-state" class="btn ghost small" type="button">Refresh</button>
          </div>
        </div>

        <div id="stats" class="stats"></div>

        <div class="split">
          <div class="panel">
            <div class="panel-head">
              <h2>Live tape</h2>
              <span id="scan-time" class="muted small"></span>
            </div>
            <div id="tape" class="tape">
              <div class="empty">No sweep yet. Press <b>Scan now</b>.</div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head">
              <h2>Chart</h2>
              <span id="chart-error" class="muted small error"></span>
            </div>
            <div id="chart" class="chart">
              <div class="empty">Waiting for a symbol. Select one above.</div>
            </div>
            <div class="chart-legend">
              <span><i class="dot"></i> latest price</span>
              <span><i class="dot dot-bull"></i> bias</span>
            </div>
          </div>
        </div>

        <div class="split">
          <div class="panel">
            <div class="panel-head">
              <h2>Bias &amp; prediction</h2>
            </div>
            <div id="forecast" class="forecast">
              <div class="empty">Awaiting a scan.</div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head">
              <h2>Signal feed</h2>
              <span id="feed-count" class="muted small">0</span>
            </div>
            <ul id="events" class="events">
              <li class="empty">Quiet.</li>
            </ul>
          </div>
        </div>
      </section>

'''
s = s[:old_start] + new + s[old_end:]

open(p, 'w', encoding='utf-8').write(s)
print('rewrote dashboard panel')
