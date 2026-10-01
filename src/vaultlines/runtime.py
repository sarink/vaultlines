"""runtime.json: everything the hook needs, so it never parses TOML or asks GitHub.

Written by `vl apply` and the daily check, to ~/.local/state/vaultlines/runtime.json.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import plugins
from .audience import Audience
from .config import Config, config_path
from .hook import RUNTIME_VERSION
from .util import contract, expand, fetch_dir, read_json, state_dir, write_json

VERSION = RUNTIME_VERSION


def path() -> Path:
    return state_dir() / "runtime.json"


def build(cfg: Config, auds: dict[str, Audience], me: str, plugin_data: dict[str, dict]) -> dict:
    """`plugin_data` is each plugin's `data()`, by plugin name."""
    filled = {s["vault"]: s["kind"] for _, _, s in plugins.sources(cfg)}
    vaults = {}
    for name, v in sorted(cfg.vaults.items()):
        a = auds.get(name)
        vaults[name] = {
            "paths": [str(v.path)],
            "show": contract(v.path),
            "audience": ({"kind": a.kind, "logins": list(a.logins), "reason": a.reason} if a
                         else {"kind": "unknown", "reason": "not checked yet"}),
        }
        if name in filled:  # a source fills it on this computer
            vaults[name].update(source=filled[name], fetch=[str(expand(fetch_dir(name)))])
    return {
        "version": VERSION,
        "written_at": time.time(),
        "config": str(config_path()),
        "me": me,
        "on_leak": cfg.on_leak,
        "vaults": vaults,
        "folders": {f.path: {"writes": f.writes, "reads": f.reads} for f in cfg.listed()},
        "plugins": {name: {"kind": cfg.plugins[name]["kind"],
                           "tool_prefixes": list(getattr(plugins.KINDS[cfg.plugins[name]["kind"]], "TOOL_PREFIXES", ())),
                           "data": d}
                    for name, d in sorted(plugin_data.items())},
    }


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
