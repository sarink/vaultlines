"""Register vaults in Obsidian's vault switcher."""

from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

from .util import home, read_json, run, write_json


def _config() -> Path:
    return home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"


def installed() -> bool:
    return sys.platform == "darwin" and _config().exists()


def running() -> bool:
    return run(["pgrep", "-x", "Obsidian"], check=False).returncode == 0


def missing(paths: list[Path]) -> list[Path]:
    known = {Path(v.get("path", "")) for v in read_json(_config(), {}).get("vaults", {}).values()}
    return [p for p in paths if p not in known]


def relocate(moved: dict[str, str]) -> bool:
    """Point Obsidian's vaults that moved (old folder -> new) to their new folders.
    Returns False if Obsidian is open, so nothing changed."""
    if not installed() or running():
        return False
    data = read_json(_config(), {})
    for v in data.get("vaults", {}).values():
        if v.get("path") in moved:
            v["path"] = moved[v["path"]]
    write_json(_config(), data)
    return True


def register(paths: list[Path]) -> list[Path]:
    """Add vaults Obsidian doesn't know yet. Returns the ones that still need adding.

    Obsidian rewrites this file when it quits, so it's only edited while Obsidian is closed.
    """
    if not installed():
        return []
    todo = missing(paths)
    if not todo or running():
        return todo
    data = read_json(_config(), {})
    vaults = data.setdefault("vaults", {})
    now = int(time.time() * 1000)
    for p in todo:
        vaults[secrets.token_hex(8)] = {"path": str(p), "ts": now}
    write_json(_config(), data)
    return []
