"""TradeVMx desktop :: environment-backed settings store.

The desktop app lets the operator paste API keys and tune risk thresholds, so
something has to write ``.env`` without destroying comments, ordering or keys it
does not own.  This module is that writer plus the form schema the UI renders.

Guarantees
----------
* atomic writes (temp file + ``os.replace``) with a ``.env.bak`` copy,
* ``0600`` permissions on POSIX,
* secrets are never returned to the browser - only ``is_set`` and a short mask,
* only keys declared in :data:`FIELD_SPECS` may be written (no arbitrary env
  injection through the API),
* values are validated and canonicalised before they touch disk.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
EXAMPLE_PATH = BASE_DIR / ".env.example"

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_TICKER_RE = re.compile(r"^(?:\^[A-Z]+|[A-Z0-9&\-]{1,20})(?:\.NS)?$")


class SettingsValidationError(ValueError):
    """Raised with per-field messages when the submitted form is invalid."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


# ---------------------------------------------------------------------------
# Form schema
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    group: str
    kind: str = "text"  # text | secret | int | float | bool | csv
    help: str = ""
    placeholder: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    #: Accepted by the API but never rendered in the form (e.g. SETUP_DONE).
    hidden: bool = False


GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        "telegram",
        "Telegram",
        "Instant push alerts and the /report PDF document. Create a bot with @BotFather.",
    ),
    (
        "email",
        "Email",
        "Gmail SMTP with a 16-character App Password. Used for severe alerts and the daily wrap.",
    ),
    (
        "groq",
        "Groq intelligence",
        "LLM verdicts. Without a key the desk falls back to deterministic ATR plans.",
    ),
    (
        "universe",
        "Watchlist",
        "NSE equities to scan. NIFTY 50 and BANK NIFTY are always included.",
    ),
    (
        "engine",
        "Scanning & risk",
        "Sweep cadence and the thresholds that decide what gets alerted.",
    ),
    (
        "desktop",
        "Desktop experience",
        "Local toasts and optional voice announcements. Both are optional extras.",
    ),
)


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        key="TELEGRAM_BOT_TOKEN",
        label="Bot token",
        group="telegram",
        kind="secret",
        placeholder="123456789:AA...",
        help="From @BotFather. The bot must be started by every chat listed below.",
    ),
    FieldSpec(
        key="TELEGRAM_CHAT_IDS",
        label="Chat IDs",
        group="telegram",
        kind="csv",
        placeholder="123456789, -1001234567890",
        help="Your personal id from @userinfobot. Groups and channels are negative.",
    ),
    FieldSpec(
        key="GMAIL_USER",
        label="Gmail address",
        group="email",
        kind="text",
        placeholder="you@gmail.com",
        help="Sender account. SMTP host is smtp.gmail.com:465.",
    ),
    FieldSpec(
        key="GMAIL_APP_PASSWORD",
        label="Gmail app password",
        group="email",
        kind="secret",
        placeholder="16 characters, no spaces",
        help="Google Account > Security > 2-Step Verification > App passwords.",
    ),
    FieldSpec(
        key="EMAIL_RECIPIENTS",
        label="Recipients",
        group="email",
        kind="csv",
        placeholder="you@gmail.com, desk@example.com",
        help="Leave empty to send only to the Gmail address above.",
    ),
    FieldSpec(
        key="ENABLE_EMAIL",
        label="Enable email channel",
        group="email",
        kind="bool",
        help="Turn off to keep everything on Telegram and in the app.",
    ),
    FieldSpec(
        key="GROQ_API_KEY",
        label="Groq API key",
        group="groq",
        kind="secret",
        placeholder="gsk_...",
        help="Free tier is enough for a desk bot: console.groq.com/keys.",
    ),
    FieldSpec(
        key="GROQ_MODEL",
        label="Model",
        group="groq",
        kind="text",
        placeholder="llama-3.3-70b-versatile",
        help="Any chat model available to your Groq account.",
    ),
    FieldSpec(
        key="WATCHLIST",
        label="Equities",
        group="universe",
        kind="csv",
        placeholder="RELIANCE.NS, HDFCBANK.NS, TCS.NS",
        help="Comma separated. Indices are added automatically.",
    ),
    FieldSpec(
        key="SCAN_INTERVAL_MINUTES",
        label="Sweep interval (minutes)",
        group="engine",
        kind="int",
        minimum=1,
        maximum=59,
        help="15 is the default. Only runs inside 09:15-15:30 IST.",
    ),
    FieldSpec(
        key="VOLUME_SURGE_MULTIPLE",
        label="Volume surge multiple",
        group="engine",
        kind="float",
        minimum=1.0,
        maximum=10.0,
        help="Alert when a bar's volume exceeds this multiple of the 10-bar SMA.",
    ),
    FieldSpec(
        key="ALERT_MIN_SEVERITY",
        label="Alert severity floor",
        group="engine",
        kind="int",
        minimum=1,
        maximum=5,
        help="1-5. Lower means more alerts; 3 is a sane desk default.",
    ),
    FieldSpec(
        key="ALERT_COOLDOWN_MINUTES",
        label="Alert cooldown",
        group="engine",
        kind="int",
        minimum=0,
        maximum=240,
        help="Suppress repeats of the same symbol/setup for this many minutes.",
    ),
    FieldSpec(
        key="MIN_RISK_REWARD",
        label="Minimum reward:risk",
        group="engine",
        kind="float",
        minimum=1.0,
        maximum=6.0,
        help="Plans below this ratio are downgraded to NO TRADE.",
    ),
    FieldSpec(
        key="MARKET_HOLIDAYS",
        label="Trading holidays",
        group="engine",
        kind="csv",
        placeholder="2026-01-26, 2026-03-04",
        help="YYYY-MM-DD list so the sentinel stays quiet on NSE holidays.",
    ),
    FieldSpec(
        key="DESKTOP_TOAST",
        label="Desktop toasts",
        group="desktop",
        kind="bool",
        help="Native notifications via the optional 'plyer' package.",
    ),
    FieldSpec(
        key="VOICE_ALERTS",
        label="Voice announcements",
        group="desktop",
        kind="bool",
        help="Speaks alert headlines via the optional 'pyttsx3' package.",
    ),
    FieldSpec(
        key="AUTO_ARM",
        label="Arm automatically on launch",
        group="desktop",
        kind="bool",
        help="Start the sentinel as soon as the app opens, so alerts arrive without pressing anything.",
    ),
    FieldSpec(
        key="UPDATE_REPO",
        label="Update repository",
        group="desktop",
        kind="text",
        placeholder="yourname/tradevmx",
        help="GitHub owner/repo used for in-app update checks. Leave empty to disable them.",
    ),
    FieldSpec(key="SETUP_DONE", label="Setup completed", group="desktop", kind="bool", hidden=True),
)

SPEC_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in FIELD_SPECS}

#: Sensible fallbacks shown in the UI when ``.env`` has no entry yet.
DEFAULTS: dict[str, str] = {
    "ENABLE_EMAIL": "true",
    "GROQ_MODEL": "llama-3.3-70b-versatile",
    "SCAN_INTERVAL_MINUTES": "15",
    "VOLUME_SURGE_MULTIPLE": "1.5",
    "ALERT_MIN_SEVERITY": "3",
    "ALERT_COOLDOWN_MINUTES": "45",
    "MIN_RISK_REWARD": "2.0",
    "DESKTOP_TOAST": "true",
    "VOICE_ALERTS": "false",
    "AUTO_ARM": "true",
    "SETUP_DONE": "false",
    "UPDATE_REPO": "LogicalArnesh/tradevmx",
}


# ---------------------------------------------------------------------------
# Parsing / serialising
# ---------------------------------------------------------------------------
def parse_env(text: str) -> dict[str, str]:
    """Minimal dotenv parser: comments, ``export``, single/double quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        key, separator, raw = stripped.partition("=")
        key = key.strip()
        if not separator or not _KEY_RE.fullmatch(key):
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


def read_env(path: Path | None = None) -> dict[str, str]:
    target = path or ENV_PATH
    try:
        return parse_env(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError:
        return {}


def _serialise(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if text == "" or "\n" in text or any(ch in text for ch in ' #"\'\t'):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def write_env(updates: dict[str, str], path: Path | None = None) -> Path:
    """Merge ``updates`` into ``.env`` atomically, preserving everything else."""
    target = path or ENV_PATH
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
        if EXAMPLE_PATH.exists():
            existing = EXAMPLE_PATH.read_text(encoding="utf-8")

    lines = existing.splitlines()
    applied: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            rendered.append(f"{key}={_serialise(updates[key])}")
            applied.add(key)
        else:
            rendered.append(line)
    for key, value in updates.items():
        if key not in applied:
            rendered.append(f"{key}={_serialise(value)}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # ``Path.with_suffix`` mangles dotfiles (``.env`` -> ``.env.env.bak``),
        # so build the backup name explicitly.
        backup = target.parent / f"{target.name}.bak"
        with contextlib.suppress(OSError):
            shutil.copy2(target, backup)

    text = "\n".join(rendered).rstrip() + "\n"
    handle, temp_name = tempfile.mkstemp(prefix=".env.", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        if hasattr(os, "chmod"):
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise
    return target


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _normalise_value(spec: FieldSpec, raw: Any) -> str:
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return "true"
        if text in {"0", "false", "no", "off", ""}:
            return "false"
        raise ValueError("must be true or false")

    text = str(raw if raw is not None else "").strip()

    if spec.kind == "int":
        if text == "":
            return DEFAULTS.get(spec.key, "0")
        try:
            value = int(float(text))
        except ValueError:
            raise ValueError("must be a whole number") from None
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"must be at least {int(spec.minimum)}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"must be at most {int(spec.maximum)}")
        return str(value)

    if spec.kind == "float":
        if text == "":
            return DEFAULTS.get(spec.key, "0")
        try:
            value = float(text)
        except ValueError:
            raise ValueError("must be a number") from None
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"must be at least {spec.minimum:g}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"must be at most {spec.maximum:g}")
        return f"{value:g}"

    if spec.kind == "csv":
        parts = [chunk.strip() for chunk in re.split(r"[,\s;]+", text) if chunk.strip()]
        if spec.key == "TELEGRAM_CHAT_IDS":
            for part in parts:
                if not re.fullmatch(r"-?\d{5,20}", part):
                    raise ValueError(f"'{part}' is not a numeric chat id")
        elif spec.key == "EMAIL_RECIPIENTS":
            for part in parts:
                if not _EMAIL_RE.match(part):
                    raise ValueError(f"'{part}' is not a valid email")
        elif spec.key == "WATCHLIST":
            for part in parts:
                if not _TICKER_RE.match(part.upper()):
                    raise ValueError(f"'{part}' is not an NSE ticker")
            parts = [part.upper() for part in parts]
        elif spec.key == "MARKET_HOLIDAYS":
            for part in parts:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
                    raise ValueError(f"'{part}' must be YYYY-MM-DD")
        return ",".join(parts)

    if spec.kind == "secret" and text and "\n" in text:
        raise ValueError("must be a single line")
    return text


def validate_updates(updates: dict[str, Any]) -> dict[str, str]:
    """Return canonical values or raise :class:`SettingsValidationError`."""
    errors: dict[str, str] = {}
    canonical: dict[str, str] = {}
    for key, raw in updates.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            errors[key] = "unknown setting"
            continue
        try:
            canonical[key] = _normalise_value(spec, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        raise SettingsValidationError(errors)
    return canonical


# ---------------------------------------------------------------------------
# Snapshot for the UI
# ---------------------------------------------------------------------------
def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "\u2022" * len(value)
    return f"{value[:2]}\u2022\u2022\u2022\u2022{value[-4:]}"


def snapshot(path: Path | None = None) -> dict[str, Any]:
    """Schema + current values with every secret masked."""
    current = read_env(path)
    fields: list[dict[str, Any]] = []
    for spec in FIELD_SPECS:
        if spec.hidden:
            continue  # written by the setup wizard, not a user-facing control
        raw = current.get(spec.key, "")
        is_set = bool(raw.strip())
        payload: dict[str, Any] = {
            "key": spec.key,
            "label": spec.label,
            "group": spec.group,
            "kind": spec.kind,
            "help": spec.help,
            "placeholder": spec.placeholder,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "is_set": is_set,
            "is_secret": spec.kind == "secret",
        }
        if spec.kind == "secret":
            payload["value"] = ""
            payload["preview"] = _mask(raw) if is_set else ""
        else:
            payload["value"] = raw if is_set else DEFAULTS.get(spec.key, "")
            payload["preview"] = ""
        fields.append(payload)

    groups = [
        {"id": gid, "title": title, "description": description}
        for gid, title, description in GROUPS
    ]
    return {
        "groups": groups,
        "fields": fields,
        "env_path": str(path or ENV_PATH),
        "readiness": {
            "telegram": bool(current.get("TELEGRAM_BOT_TOKEN")) and bool(current.get("TELEGRAM_CHAT_IDS")),
            "email": bool(
                current.get("GMAIL_USER")
                and current.get("GMAIL_APP_PASSWORD")
                and (current.get("ENABLE_EMAIL", "true").lower() in {"1", "true", "yes", "on"})
            ),
            "groq": bool(current.get("GROQ_API_KEY")),
        },
    }


def save(updates: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Validate, persist and return the refreshed public snapshot."""
    canonical = validate_updates(updates)
    if canonical:
        write_env(canonical, path)
    return snapshot(path)
