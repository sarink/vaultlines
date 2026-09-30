"""runtime.json: everything the hook needs, so it never parses TOML or asks GitHub.

Written by `vl apply` and the daily check, to ~/.local/state/vaultlines/runtime.json.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .audience import Audience
from .config import Config, config_path
from .util import contract, read_json, state_dir, write_json

VERSION = 1


def path() -> Path:
    return state_dir() / "runtime.json"


def build(cfg: Config, auds: dict[str, Audience], me: str,
          bm_projects: dict[str, Path] | None, plugin: bool) -> dict:
    """`bm_projects` is every Basic Memory project (name -> folder), or None if the adapter is off."""
    vaults = {}
    for name, v in sorted(cfg.vaults.items()):
        a = auds.get(name)
        vaults[name] = {
            "paths": [str(v.path)],
            "show": contract(v.path),
            "audience": ({"kind": a.kind, "logins": list(a.logins), "reason": a.reason} if a
                         else {"kind": "unknown", "reason": "not checked yet"}),
        }
    by_path = {str(v.path): name for name, v in cfg.vaults.items()}
    data = {
        "version": VERSION,
        "written_at": time.time(),
        "config": str(config_path()),
        "me": me,
        "on_leak": cfg.on_leak,
        "vaults": vaults,
        "folders": {f.path: {"writes": f.writes, "reads": f.reads} for f in cfg.listed()},
        "basic_memory": None,
    }
    if bm_projects is not None:
        data["basic_memory"] = {
            "plugin": plugin,
            "projects": {p: by_path.get(str(folder)) for p, folder in sorted(bm_projects.items())},
        }
    return data


def write(data: dict) -> None:
    write_json(path(), data)


def load() -> dict | None:
    try:
        return read_json(path(), None)
    except (OSError, json.JSONDecodeError):
        return None


def stale(data: dict | None) -> str | None:
    """Why runtime.json is out of date, or None."""
    if data is None:
        return "runtime.json is missing"
    if data.get("version") != VERSION:
        return "runtime.json is from another version of vl"
    cfg = config_path()
    if cfg.exists() and cfg.stat().st_mtime > data.get("written_at", 0):
        return "the config changed after runtime.json was written"
    return None
