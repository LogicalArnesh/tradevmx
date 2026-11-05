"""Tests for the in-app update checker.

No network: ``updater._fetch`` is replaced everywhere. The point of these tests
is that every failure mode degrades to "no update available" instead of raising.
"""

from __future__ import annotations

import urllib.error

import pytest

import updater


def test_parse_version_reads_tags() -> None:
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("release-2.0") == (2, 0)
    assert updater.parse_version("nightly") == ()
    assert updater.parse_version(None) == ()


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("v1.0.1", "1.0.0", True),
        ("v1.0.0", "1.0.0", False),
        ("v2", "1.9.9", True),
        ("v1.0", "1.0.1", False),
        ("v1.10.0", "1.9.0", True),
        ("garbage", "1.0.0", False),
    ],
)
def test_is_newer(latest: str, current: str, expected: bool) -> None:
    assert updater.is_newer(latest, current) is expected


def test_check_without_repo_is_quiet() -> None:
    payload = updater.check("", "1.0.0")
    assert payload["configured"] is False
    assert payload["available"] is False
    assert payload["error"]


def test_check_treats_missing_release_as_normal(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://api.github.com", 404, "Not Found", {}, None)

    monkeypatch.setattr(updater, "_fetch", boom)
    payload = updater.check("someone/tradevmx", "1.0.0")
    assert payload["available"] is False
    assert payload["latest"] is None
    assert "no releases" in str(payload["error"])


def test_check_explains_a_rate_limit(monkeypatch) -> None:
    def limited(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://api.github.com", 403, "Forbidden", {}, None)

    monkeypatch.setattr(updater, "_fetch", limited)
    payload = updater.check("someone/tradevmx", "1.0.0")
    assert payload["available"] is False
    assert "rate limit" in str(payload["error"]).lower()


def test_check_survives_offline(monkeypatch) -> None:
    def offline(*_args, **_kwargs):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(updater, "_fetch", offline)
    payload = updater.check("someone/tradevmx", "1.0.0")
    assert payload["available"] is False
    assert "GitHub" in str(payload["error"])


def test_check_reports_a_newer_release(monkeypatch) -> None:
    monkeypatch.setattr(
        updater,
        "_fetch",
        lambda repo, timeout: {
            "tag_name": "v1.1.0",
            "html_url": "https://github.com/someone/tradevmx/releases/tag/v1.1.0",
            "published_at": "2026-02-01T10:00:00Z",
            "body": "Faster sweeps.",
        },
    )
    payload = updater.check("someone/tradevmx", "1.0.0")
    assert payload["available"] is True
    assert payload["latest"] == "v1.1.0"
    assert payload["url"].endswith("v1.1.0")
    assert payload["notes"] == "Faster sweeps."


def test_check_does_not_offer_the_current_version(monkeypatch) -> None:
    monkeypatch.setattr(
        updater, "_fetch", lambda repo, timeout: {"tag_name": "v1.0.0", "html_url": "u"}
    )
    payload = updater.check("someone/tradevmx", "1.0.0")
    assert payload["available"] is False


def test_cache_avoids_repeated_requests(monkeypatch) -> None:
    calls: list[str] = []

    def fake(repo: str, timeout: float):
        calls.append(repo)
        return {"tag_name": "v9.9.9", "html_url": "https://example.invalid"}

    monkeypatch.setattr(updater, "_fetch", fake)
    updater.reset_cache()

    first = updater.check_cached("cache-test/repo", "1.0.0", ttl=3600)
    second = updater.check_cached("cache-test/repo", "1.0.0", ttl=3600)
    assert first["available"] is True
    assert second == first
    assert len(calls) == 1

    updater.check_cached("cache-test/repo", "1.0.0", ttl=3600, force=True)
    assert len(calls) == 2
