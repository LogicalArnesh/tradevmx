"""Cut a TradeXVm release.

    python release.py 1.0.1 "Tighten the opening-brief window"

Bumps ``version.py``, commits everything, and creates the annotated tag
``v1.0.1``.  It never pushes: review the diff, then run the printed command.
Pushing the tag is what makes GitHub build the downloadable zip and what the
in-app update banner notices.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "version.py"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _run(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _write_version(version: str) -> None:
    text = VERSION_FILE.read_text(encoding="utf-8")
    updated, count = re.subn(r'__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"', text)
    if count != 1:
        raise SystemExit("Could not find the __version__ assignment in version.py")
    VERSION_FILE.write_text(updated, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bump TradeXVm and tag a release")
    parser.add_argument("version", help="new version, e.g. 1.0.1")
    parser.add_argument("message", nargs="?", default="", help="release note headline")
    args = parser.parse_args(argv)

    if not _SEMVER.match(args.version):
        raise SystemExit("Version must look like 1.0.1 (no leading 'v').")

    tag = f"v{args.version}"
    existing = _run("tag", "--list", tag)
    if existing:
        raise SystemExit(f"Tag {tag} already exists.")

    _write_version(args.version)
    _run("add", "-A")
    headline = args.message.strip() or f"Release {tag}"
    _run("commit", "-m", f"{headline}\n\nBump version to {args.version}.")
    _run("tag", "-a", tag, "-m", headline)

    print(f"Bumped to {args.version} and tagged {tag}.")
    print("Publish it with:")
    print(f"  git push origin HEAD --tags")
    print("GitHub Actions then builds the zip and the in-app updater sees the tag.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
