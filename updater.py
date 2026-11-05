"""TradeXVm :: in-app update checks.

The console is local-first, so this module is deliberately forgiving: a missing
repository, no published releases, no network or a rate limit all resolve to a
plain "nothing to install" answer instead of an exception.  Results are cached
for a few hours so polling the UI cannot burn the anonymous GitHub quota.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOGGER = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT_SECONDS = 6.0
CACHE_TTL_SECONDS = 6 * 60 * 60

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")

_cache: dict[str, tuple[float, dict[str, object]]] = {}
_lock = threading.Lock()


def parse_version(text: str) -> tuple[int, ...]:
    """``v1.2.3`` -> ``(1, 2, 3)``; unparseable input -> ``()``."""
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    """Semantic-ish comparison that tolerates missing components."""
    new, old = parse_version(latest), parse_version(current)
    if not new or not old:
        return False
    width = max(len(new), len(old))
    padded_new = new + (0,) * (width - len(new))
    padded_old = old + (0,) * (width - len(old))
    return padded_new > padded_old


def _fetch(repo: str, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(
        RELEASES_URL.format(repo=repo),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "TradeXVm-updater",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check(repo: str, current: str, *, timeout: float = TIMEOUT_SECONDS) -> dict[str, object]:
    """Look up the latest release.  Never raises."""
    payload: dict[str, object] = {
        "repo": repo,
        "current": current,
        "configured": bool(repo.strip()),
        "latest": None,
        "available": False,
        "url": "",
        "notes": "",
        "published_at": None,
        "error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not repo.strip():
        payload["error"] = "no update repository configured"
        return payload

    try:
        release = _fetch(repo.strip(), timeout)
    except urllib.error.HTTPError as exc:
        payload["error"] = (
            "no releases published yet" if exc.code == 404 else f"GitHub returned {exc.code}"
        )
        return payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        payload["error"] = f"could not reach GitHub ({getattr(exc, 'reason', exc)})"
        return payload
    except ValueError as exc:  # covers JSONDecodeError and odd payloads
        payload["error"] = f"unreadable release metadata ({exc})"
        return payload

    tag = str(release.get("tag_name") or release.get("name") or "").strip()
    payload["latest"] = tag or None
    payload["url"] = str(
        release.get("html_url") or (f"https://github.com/{repo}/releases" if tag else "")
    )
    payload["published_at"] = release.get("published_at")
    payload["notes"] = str(release.get("body") or "")[:1200]
    payload["available"] = bool(tag) and is_newer(tag, current)
    return payload


def check_cached(
    repo: str,
    current: str,
    *,
    ttl: float = CACHE_TTL_SECONDS,
    force: bool = False,
    timeout: float = TIMEOUT_SECONDS,
) -> dict[str, object]:
    key = f"{repo.strip().lower()}@{current}"
    now = datetime.now(timezone.utc).timestamp()
    with _lock:
        cached = _cache.get(key)
    if cached is not None and not force and now - cached[0] < ttl:
        return cached[1]
    result = check(repo, current, timeout=timeout)
    with _lock:
        _cache[key] = (now, result)
    return result


def reset_cache() -> None:
    """Test helper: forget every cached answer."""
    with _lock:
        _cache.clear()
