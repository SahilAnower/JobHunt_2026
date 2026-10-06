#!/usr/bin/env python3
"""
Load config/profile.yaml, in one place.

Six scripts need the profile — fetch, score, draft, tailor, digest, discover — and each
carried its own `PROFILE = ROOT / "config" / "profile.yaml"` plus its own yaml.safe_load. That
is harmless until the handling diverges, which it had: fetch.py tolerated a missing
runtime.user_agent and fell back to "jobhunt/1.0", while discover.py raised KeyError on the
same profile. The tolerant version is the one kept here.

A module, not a package, and deliberately small. It holds the profile and nothing else: each
script keeps its own ROOT, because each resolves its own output directory against it (outbox/,
tailored/, digest/) and reports paths relative to it.

    python3 jobprofile.py      # print the runtime block, to check the file parses
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "config" / "profile.yaml"

DEFAULT_UA = "jobhunt/1.0"


def load(path: Path = PROFILE) -> dict:
    """The whole profile. Takes a path so a test can hand it a fixture instead."""
    return yaml.safe_load(path.read_text())


def runtime(profile: dict) -> dict:
    """
    The `runtime:` block, or an empty dict. Callers read it with .get and their own default,
    which keeps each default next to the code that depends on it.

    The `or {}` matters: a profile with a `runtime:` key and nothing under it parses to None,
    not to {}, and `.get` on None is an AttributeError at startup.
    """
    return profile.get("runtime") or {}


def user_agent(profile: dict) -> str:
    """
    The UA, collapsed to one line.

    profile.example.yaml wraps the value across two YAML lines, so it arrives with a newline
    and leading spaces in the middle. Sending that raw is a malformed header. Collapsing the
    whitespace is what every caller was doing by hand.
    """
    return " ".join((runtime(profile).get("user_agent") or DEFAULT_UA).split())


if __name__ == "__main__":
    if not PROFILE.exists():
        sys.exit(f"no profile at {PROFILE} — copy config/profile.example.yaml")
    p = load()
    print(f"{PROFILE.name} parsed. runtime: {runtime(p)}")
    print(f"user_agent: {user_agent(p)}")
