"""Single source of truth for the TradeXVm version.

Bump it with ``python release.py 1.2.0`` (which also commits and tags), never by
hand in several places.  The in-app updater compares this string with the latest
GitHub release tag, so the tag must look like ``v1.2.0``.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
