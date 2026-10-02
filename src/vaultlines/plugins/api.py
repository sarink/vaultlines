"""What a plugin hands back to the hook. Standard library only: the hook imports it."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..util import inside


@dataclass
class Access:
    """Which vaults a tool call touches, as a plugin sees it. vl decides what's allowed."""
    kind: str = "read"  # "read" or "write"
    vaults: list[str] = field(default_factory=list)
    block: str | None = None  # the call can't be checked, so it's refused
    updated_input: dict | None = None


def here(rules: dict | None) -> str:
    """The vaults a session uses (its rules: {"writes", "reads"}), for messages."""
    used = [v for v in [(rules or {}).get("writes"), *((rules or {}).get("reads") or [])] if v]
    if not used:
        return "No vaults are set up here."
    return f"This session uses {', '.join(used)}."


def mentions(command: str, path: str) -> bool:
    """Whether a shell command names `path`: as it is, or as ~/..., $HOME/... or ${HOME}/..."""
    forms = [path]
    home = os.path.expanduser("~")
    for h in {home, os.path.realpath(home)}:
        if inside(path, h) and path != h:
            rel = path[len(h) + 1:]
            forms += [f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"]
    return any(f in command for f in forms)
