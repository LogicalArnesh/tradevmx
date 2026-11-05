"""Tests for the desktop console: settings store, API contract, PDF endpoint.

Everything runs offline: the scanner is never called for real, no Telegram,
Gmail or Groq request is made.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime

import pytest

import settings_store
from ai_engine import GroqBrain
from app import DeskState, build_state, create_app
from config import DEFAULT_UPDATE_REPO, IST, get_settings
from market_scanner import MarketSnapshot, ScanReport, Signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# desk configuration\n"
        "TELEGRAM_CHAT_IDS=123456789\n"
        "TELEGRAM_BOT_TOKEN=123456:AAsecret-token-value\n"
        "WATCHLIST=RELIANCE.NS,TCS.NS\n"
        "SCAN_INTERVAL_MINUTES=15\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def desk(env_file):
    saved = dict(os.environ)
    state = DeskState(env_path=env_file)
    try:
        yield state
    finally:
        state.shutdown()
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


@pytest.fixture()
def client(desk):
    app = create_app(state=desk, token="test-token")
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="RELIANCE.NS",
        display_name="RELIANCE",
        timeframe="15m",
        as_of=datetime(2026, 1, 5, 14, 30, tzinfo=IST),
        cmp=1432.10,
        prev_close=1414.60,
        change_pct=1.24,
        day_open=1417.00,
        day_high=1438.00,
        day_low=1414.90,
        vwap=1421.35,
        vwap_source="volume weighted (session anchored)",
        atr=14.20,
        volume=1_820_000,
        volume_sma=980_000,
        volume_ratio=1.86,
        bias="BULLISH",
        bias_reason="higher highs and higher lows",
        structure="bullish",
        signals=(
            Signal(
                code="VWAP_CROSS_UP",
                label="VWAP reclaim",
                direction="bullish",
                severity=3,
                detail="15m close crossed above VWAP",
                timeframe="15m",
            ),
        ),
    )


def _report(snapshot: MarketSnapshot | None = None) -> ScanReport:
    return ScanReport(created_at=datetime(2026, 1, 5, 14, 30, tzinfo=IST), snapshots=(snapshot or _snapshot(),))


# ---------------------------------------------------------------------------
# settings store
# ---------------------------------------------------------------------------
def test_parse_env_handles_quotes_comments_and_export() -> None:
    parsed = settings_store.parse_env(
        '# comment\nA=1\nexport B="two words"\nC=\'single\'\nD=\nbad line\n'
    )
    assert parsed == {"A": "1", "B": "two words", "C": "single", "D": ""}


def test_write_env_preserves_comments_and_foreign_keys(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("# keep me\nOTHER=value\nGROQ_MODEL=old\n", encoding="utf-8")

    settings_store.write_env({"GROQ_MODEL": "new-model", "WATCHLIST": "INFY.NS, SBIN.NS"}, path)

    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "OTHER=value" in text
    assert "GROQ_MODEL=new-model" in text
    assert 'WATCHLIST="INFY.NS, SBIN.NS"' in text
    assert (tmp_path / ".env.bak").exists()
    assert settings_store.read_env(path)["WATCHLIST"] == "INFY.NS, SBIN.NS"


def test_validation_rejects_bad_values() -> None:
    with pytest.raises(settings_store.SettingsValidationError) as excinfo:
        settings_store.validate_updates(
            {
                "ALERT_MIN_SEVERITY": "9",
                "TELEGRAM_CHAT_IDS": "not-a-number",
                "EMAIL_RECIPIENTS": "nope",
                "UNKNOWN_KEY": "x",
            }
        )
    errors = excinfo.value.errors
    assert set(errors) == {"ALERT_MIN_SEVERITY", "TELEGRAM_CHAT_IDS", "EMAIL_RECIPIENTS", "UNKNOWN_KEY"}


def test_validation_normalises_booleans_and_numbers() -> None:
    assert settings_store.validate_updates({"ENABLE_EMAIL": "off"})["ENABLE_EMAIL"] == "false"
    assert settings_store.validate_updates({"VOLUME_SURGE_MULTIPLE": "2"})["VOLUME_SURGE_MULTIPLE"] == "2"
    assert settings_store.validate_updates({"WATCHLIST": "reliance.ns, tcs"})["WATCHLIST"] == "RELIANCE.NS,TCS"


def test_snapshot_never_exposes_secrets(env_file) -> None:
    snap = settings_store.snapshot(env_file)
    assert "AAsecret-token-value" not in str(snap)
    token = next(field for field in snap["fields"] if field["key"] == "TELEGRAM_BOT_TOKEN")
    assert token["is_set"] is True
    assert token["value"] == ""
    assert "\u2022" in token["preview"]
    assert snap["readiness"]["telegram"] is True


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------
def test_index_renders_with_token(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'meta name="tradevmx-token" content="test-token"' in body
    assert "TRADEVMX" in body
    assert "Content-Security-Policy" in response.headers


def test_state_endpoint_shape(client, desk) -> None:
    desk.sentinel._report = _report()
    desk.sentinel._last_scan_at = datetime(2026, 1, 5, 14, 30, tzinfo=IST)

    payload = client.get("/api/state").get_json()
    assert payload["clock"]["phase"] in {"OPEN", "CLOSED", "PRE-OPEN", "PRE-MARKET", "HOLIDAY/WEEKEND"}
    assert payload["sentinel"]["running"] is False
    assert payload["market"]["scanned"] == 1
    assert payload["market"]["rows"][0]["name"] == "RELIANCE"
    assert payload["market"]["rows"][0]["signals"][0]["label"] == "VWAP reclaim"
    assert payload["readiness"]["telegram"] is True


def test_mutations_require_token(client) -> None:
    assert client.post("/api/scan", json={}).status_code == 401
    assert client.post(
        "/api/sentinel", json={"action": "start"}, headers={"X-TradeVMx-Token": "wrong"}
    ).status_code == 401
    # A foreign Origin is refused before the token is even considered.
    assert client.post(
        "/api/settings",
        json={"values": {"ALERT_MIN_SEVERITY": "4"}},
        headers={"Origin": "http://evil.example"},
    ).status_code == 403


def test_cross_origin_is_rejected(client, desk) -> None:
    desk.sentinel.cycle_intraday = lambda: 0  # keep the scheduler from scanning
    response = client.post(
        "/api/sentinel",
        json={"action": "start"},
        headers={"X-TradeVMx-Token": "test-token", "Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_sentinel_start_stop(client, desk) -> None:
    desk.sentinel.cycle_intraday = lambda: 0  # no network sweep during tests
    started = client.post(
        "/api/sentinel", json={"action": "start"}, headers={"X-TradeVMx-Token": "test-token"}
    )
    assert started.status_code == 200
    assert started.get_json()["running"] is True
    assert started.get_json()["state"]["sentinel"]["schedule"]

    stopped = client.post(
        "/api/sentinel", json={"action": "stop"}, headers={"X-TradeVMx-Token": "test-token"}
    )
    assert stopped.status_code == 200
    assert stopped.get_json()["running"] is False


def test_scan_endpoint_uses_sentinel(client, desk) -> None:
    report = _report()

    def fake_scan(force: bool = True):
        desk.sentinel._report = report
        desk.sentinel._last_scan_at = report.created_at
        return report

    desk.sentinel.scan = fake_scan
    response = client.post("/api/scan", json={"force": True}, headers={"X-TradeVMx-Token": "test-token"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["scanned"] == 1
    assert payload["actionable"] == 1
    assert payload["state"]["market"]["rows"][0]["cmp"] == 1432.10


def test_settings_save_writes_env_and_reloads(client, desk, env_file) -> None:
    response = client.post(
        "/api/settings",
        json={"values": {"SCAN_INTERVAL_MINUTES": "20", "ALERT_MIN_SEVERITY": "4"}},
        headers={"X-TradeVMx-Token": "test-token"},
    )
    assert response.status_code == 200
    saved = env_file.read_text(encoding="utf-8")
    assert "SCAN_INTERVAL_MINUTES=20" in saved
    assert "# desk configuration" in saved
    assert desk.settings.scan_interval_minutes == 20
    assert desk.settings.alert_min_severity == 4


def test_settings_save_rejects_invalid_values(client, env_file) -> None:
    before = env_file.read_text(encoding="utf-8")
    response = client.post(
        "/api/settings",
        json={"values": {"ALERT_MIN_SEVERITY": "42"}},
        headers={"X-TradeVMx-Token": "test-token"},
    )
    assert response.status_code == 400
    assert "fields" in response.get_json()
    assert env_file.read_text(encoding="utf-8") == before


def test_report_endpoint_returns_pdf(client, desk) -> None:
    snapshot = _snapshot()
    plan = GroqBrain(desk.settings).fallback_plan(snapshot)
    artifact = desk.reports.render(snapshot, plan)
    desk.sentinel.build_report = lambda symbol: (artifact, plan, snapshot)

    response = client.get("/api/report?symbol=RELIANCE")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/pdf")
    assert response.data.startswith(b"%PDF")
    assert len(response.data) > 5_000


def test_report_endpoint_rejects_unknown_symbol(client) -> None:
    response = client.get("/api/report?symbol=not%20a%20symbol!!")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_log_endpoint_is_plain_text(client) -> None:
    response = client.get("/api/logs?lines=20")
    assert response.status_code == 200
    assert response.mimetype == "text/plain"


def test_next_sweep_skips_out_of_session_cron_slots(desk) -> None:
    pre_open = desk.sentinel._next_intraday_run(datetime(2026, 1, 5, 9, 5, tzinfo=IST))
    assert pre_open is not None and pre_open.strftime("%H:%M") == "09:15"

    after_close = desk.sentinel._next_intraday_run(datetime(2026, 1, 5, 15, 35, tzinfo=IST))
    assert after_close is not None and after_close.strftime("%a %H:%M") == "Tue 09:15"


def test_setup_flag_is_hidden_but_writable(client, env_file) -> None:
    snap = client.get("/api/settings").get_json()
    assert all(field["key"] != "SETUP_DONE" for field in snap["fields"])
    assert client.get("/api/state").get_json()["setup"]["needs_setup"] is True

    response = client.post(
        "/api/settings",
        json={"values": {"SETUP_DONE": True}},
        headers={"X-TradeVMx-Token": "test-token"},
    )
    assert response.status_code == 200
    assert "SETUP_DONE=true" in env_file.read_text(encoding="utf-8")
    assert response.get_json()["state"]["setup"]["needs_setup"] is False


def test_update_endpoint_without_repo_is_quiet(client, desk) -> None:
    # A stale or hand-cleared setting must not produce a scary banner.
    desk.settings = replace(desk.settings, update_repo="")
    payload = client.get("/api/update").get_json()
    assert payload["configured"] is False
    assert payload["available"] is False
    assert payload["current"]


def test_update_repo_defaults_to_upstream(desk) -> None:
    # A freshly unzipped release must still check for new versions.
    assert desk.settings.update_repo == DEFAULT_UPDATE_REPO
    assert "/" in DEFAULT_UPDATE_REPO


def test_build_state_without_scan_is_safe(desk) -> None:
    payload = build_state(desk)
    assert payload["market"]["scanned"] == 0
    assert payload["market"]["rows"] == []
    assert payload["events"] == []
