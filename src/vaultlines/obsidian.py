"""Keep vl's vaults in Obsidian's vault switcher."""

from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

from .util import home, read_json, run, vaults_dir, write_json


def _config() -> Path:
    return home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"


def installed() -> bool:
    return sys.platform == "darwin" and _config().exists()


def running() -> bool:
    return run(["pgrep", "-x", "Obsidian"], check=False).returncode == 0


def missing(paths: list[Path]) -> list[Path]:
    known = {Path(v.get("path", "")) for v in read_json(_config(), {}).get("vaults", {}).values()}
    return [p for p in paths if p not in known]


def _gone(vaults: dict) -> list[str]:
    """Keys of vaults in vl's vaults/ folder that aren't there anymore. Other folders aren't vl's."""
    root = vaults_dir()
    return [k for k, v in vaults.items()
            if Path(v.get("path", "")).is_relative_to(root) and not Path(v.get("path", "")).exists()]


def register(paths: list[Path]) -> list[Path]:
    """Add vaults Obsidian doesn't know yet, and drop vl's vaults that are gone. Returns the
    ones that still need adding.

    Obsidian rewrites this file when it quits, so it's only edited while Obsidian is closed.
    """
    if not installed():
        return []
    todo = missing(paths)
    data = read_json(_config(), {})
    vaults = data.setdefault("vaults", {})
    gone = _gone(vaults)
    if not (todo or gone) or running():
        return todo
    for k in gone:
        del vaults[k]
    now = int(time.time() * 1000)
    for p in todo:
        vaults[secrets.token_hex(8)] = {"path": str(p), "ts": now}
    write_json(_config(), data)
    return []
