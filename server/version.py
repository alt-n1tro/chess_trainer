"""One number that says which build you are looking at.

Semantic versioning, read from the top of CHANGELOG.md so there is exactly
one place to edit:

  MAJOR  the app works differently than you remember -- a schema change that
         rewrites stored games, a workflow that moved, anything that makes an
         old habit wrong.
  MINOR  something new you can use: a screen, a command, a kind of drill, a
         new class of explanation.
  PATCH  a fix or a polish. The app is the same app, it is just less wrong.
"""
from __future__ import annotations

import os
import re

_CHANGELOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "CHANGELOG.md")
_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)\b")


def _read() -> str:
    try:
        with open(_CHANGELOG, encoding="utf-8") as fh:
            for line in fh:
                found = _HEADING.match(line)
                if found:
                    return found.group(1)
    except OSError:
        pass
    return "0.0.0"


VERSION = _read()


def parts() -> tuple[int, int, int]:
    major, minor, patch = VERSION.split(".")
    return int(major), int(minor), int(patch)
