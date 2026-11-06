import re

p = 'app.py'
s = open(p, encoding='utf-8').read()

# 1) import chart_engine
s = s.replace(
    "from chart_engine import ChartEngine",
    "from chart_engine import ChartEngine",
)

# 2) add symbols endpoint
old = '''    @app.get("/api/logs")'''
new = '''    @app.get("/api/symbols")
    def api_symbols():
        """List the watched instruments the chart knows"""
        symbols = sorted({
            ins.symbol for ins in desk.settings.watchlist
        } | set(get_settings()._INDEX_INSTRUMENTS.keys()))
        return jsonify({
            "symbols": symbols,
            "current": desk.settings.watchlist[0].symbol if desk.settings.watchlist else None,
        })

    @app.get("/api/chart")
    def api_chart():
        """Current live chart for a symbol (bars + levels + snapshot)."""
        symbol = request.args.get("symbol", "").strip().upper()
        timeframe = request.args.get("timeframe", "15m")
        if not symbol:
            return jsonify({"error": "symbol is required"}), 400
        if timeframe not in SUPPORTED_INTERVALS:
            return jsonify({"error": f"unsupported timeframe; use {', '.join(sorted(SUPPORTED_INTERVALS))}"}), 400
        try:
            state = desk.chart_engine.state(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 502
        return jsonify({
            "symbol": state.symbol,
            "name": state.name,
            "timeframe": state.timeframe,
            "current": state.snapshot.cmp if state.snapshot else None,
            "change_pct": state.snapshot.change_pct if state.snapshot else None,
            "is_stale": state.is_stale,
            "bars": [b.as_dict() for b in state.bars],
            "levels": state.levels,
            "scanned_at": state.scanned_at,
            "error": state.error,
        })

    @app.get("/api/logs")'''
assert old in s
s = s.replace(old, new)

# 3) chart engine in DeskState + registry on startup
old = '''        self.scanner = MarketScanner(self.settings)
        self.brain = GroqBrain(self.settings)'''
new = '''        self.scanner = MarketScanner(self.settings)
        self.chart_engine = ChartEngine(self.settings)
        self.brain = GroqBrain(self.settings)'''
assert old in s
s = s.replace(old, new)

# 4) register at startup (after create_app)
old = '''    app.config["DESK"] = desk
    configure_logging(desk.settings)
    _schedule_update_check(app)'''
new = '''    app.config["DESK"] = desk
    configure_logging(desk.settings)
    _schedule_update_check(app)
    ChartEngine.register(desk.chart_engine)'''
assert old in s
s = s.replace(old, new)

open(p, 'w', encoding='utf-8').write(s)
print('patched app.py with /api/symbols, /api/chart, chart engine')
