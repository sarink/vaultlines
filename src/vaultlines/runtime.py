"""runtime.json: everything the hook needs, so it never parses TOML or asks GitHub.

Written by `vl apply` and the daily check, to ~/.vaultlines/state/runtime.json. Vaults
are keyed by their short names, which are also their Basic Memory project names.
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


def _audience(a: Audience | None) -> dict:
    if a is None:
        return {"kind": "unknown", "logins": [], "reason": "not checked yet"}
    return {"kind": a.kind, "logins": list(a.logins), "reason": a.reason}


def owners(cfg: Config, lost: set[str], warnings: list[str]) -> dict[str, dict]:
    """Each joined owner: your personal vault, its vaults, and which vault each repo's notes go to."""
    shorts = cfg.shorts
    out = {}
    for owner in cfg.owners:
        mine = sorted(vid for vid in cfg.vaults if vid.split("/")[0] == owner and vid not in lost)
        claims: dict[str, list[str]] = {}
        for vid in mine:
            v = cfg.vaults[vid]
            if v.source is not None and v.notes_from:
                warnings.append(f"{vid}: notes_from is ignored, because the vault comes from {v.source.get('kind')} "
                                "and can't take notes")
                continue
            for repo in v.notes_from:
                if repo.split("/")[0] != owner:
                    warnings.append(f"{vid}: notes_from lists {repo}, which belongs to another owner, so it's ignored "
                                    "(owners are kept apart)")
                    continue
                claims.setdefault(repo, []).append(shorts[vid])
        personal = cfg.personal(owner)
        out[owner] = {
            "personal": shorts[personal] if personal else None,
            "vaults": [shorts[vid] for vid in mine],
            "notes_from": {repo: c[0] for repo, c in sorted(claims.items()) if len(c) == 1},
            "conflicts": {repo: sorted(c) for repo, c in sorted(claims.items()) if len(c) > 1},
        }
    return out


def _rule(cfg: Config, rule, where: str, lost: set[str], warnings: list[str]) -> dict:
    shorts = cfg.shorts

    def ref(vid: str | None, key: str) -> str | None:
        if vid is None:
            return None
        if vid not in cfg.vaults:
            warnings.append(f"config.toml: {where}.{key}: no vault {vid} on this computer, so it's left out")
            return None
        if vid in lost:
            warnings.append(f"config.toml: {where}.{key}: you can't access {vid} on GitHub any more, so it's left out")
            return None
        return shorts[vid]

    writes = ref(rule.writes, "writes")
    if writes and cfg.vaults[rule.writes].source is not None:
        warnings.append(f"config.toml: {where}.writes: {rule.writes} comes from "
                        f"{cfg.vaults[rule.writes].source.get('kind')}, so notes can't be saved there. Put it in reads.")
        writes = None
    reads = [s for s in (ref(r, "reads") for r in rule.reads) if s]
    out = {"writes": writes, "reads": reads}
    if getattr(rule, "allow_vl_commands", False):
        out["allow_vl_commands"] = True
    return out


def build(cfg: Config, auds: dict[str, Audience], plugin_data: dict[str, dict],
          lost: set[str] = frozenset(), warnings: list[str] | None = None) -> dict:
    """`plugin_data` is each plugin's `data()`, by plugin name. `lost`: vaults you can't access
    on GitHub any more. Problems are added to `warnings`."""
    warnings = [] if warnings is None else warnings
    shorts = cfg.shorts
    vaults = {}
    for vid, v in sorted(cfg.vaults.items()):
        entry = {"id": vid, "paths": [str(expand(v.path))], "show": contract(v.path), "about": v.about,
                 "audience": _audience(auds.get(vid))}
        if v.source is not None:
            entry.update(source=v.source.get("kind") or "unknown", fetch=[str(expand(fetch_dir(vid)))])
        if vid in lost:
            entry["lost"] = True
        vaults[shorts[vid]] = entry
    mine = cfg.personal(cfg.me) if cfg.me else None
    return {
        "version": VERSION,
        "written_at": time.time(),
        "config": str(config_path()),
        "me": cfg.me,
        "on_leak": cfg.on_leak,
        "vaults": vaults,
        "owners": owners(cfg, lost, warnings),
        "repos": {key: _rule(cfg, r, f'repos."{key}"', lost, warnings) for key, r in sorted(cfg.repos.items())},
        "folders": {key: _rule(cfg, r, f'folders."{contract(key)}"', lost, warnings)
                    for key, r in sorted(cfg.folders.items())},
        "default": {"writes": shorts[mine] if mine else None, "reads": []},
        "plugins": {name: {"kind": name,
                           "tool_prefixes": list(getattr(plugins.KINDS[name], "TOOL_PREFIXES", ())),
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
        return "config.toml changed after runtime.json was written"
    return None
