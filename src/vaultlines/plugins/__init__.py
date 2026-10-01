"""Plugins: built-in extras that report facts to vl. vl's core decides about access.

A plugin is a module. Every function is optional.

The hook side is pure, fast and uses only the standard library, because `vl hook`
imports it. `data` is what the plugin put in runtime.json:

  TOOL_PREFIXES                           tool names the plugin answers for
  on_call(tool, args, folder, data)       -> Access: which vaults a call touches
  context_vaults(project_dir, data)       -> vaults already put into a new session
  briefing(writes, data)                  -> (text inside the parentheses, sentence at the end)
                                             of the SessionStart message
  guard(tool, args, cwd, data)            -> a reason to deny any call, whatever the folder
  source_briefing(vault)                  -> a sentence for the SessionStart message, for each
                                             vault this source fills that the folder uses

The vl side imports what it needs inside each function. `settings` is the plugin's
table in the config:

  KEYS, validate(settings)                the config keys besides `kind`; the first problem
  init(cfg, settings)                     one-time setup, from `vl init`
  apply(cfg, settings, state, warnings)   -> new state: make the world match the config.
                                             `state` is what the last apply returned.
  off(state, warnings)                    undo apply, after the plugin left the config
  data(cfg, settings)                     -> the plugin's part of runtime.json
  vault_removed(cfg, settings, name)      clean up after `vl vault remove`
  doctor(cfg, settings, check)            print a section of `vl doctor`

A plugin with `run` is a source: it fills one vault, on a timer, from one computer. It
only copies, and knows nothing about access; the vault's GitHub repo decides who sees
what it wrote. vl adds the keys `vault` (required) and `every` (seconds between runs)
to its table.

  run(cfg, settings, vault, rebuild)      -> a short status: fill `vault.path`. VlError on failure.
                                             vl commits what changed, then syncs the vault.
                                             `rebuild`: write every file again.
  fetch(cfg, settings, vault, path, dest) -> the local copy of one original, under `dest`
  EVERY                                   the default seconds between runs
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import ModuleType

from ..util import VlError, machine_id
from . import basic_memory, drive

KINDS: dict[str, ModuleType] = {"basic-memory": basic_memory, "drive": drive}
SOURCE_KEYS = {"vault", "every"}

# Every built-in prefix, on or off, so the hook's matcher doesn't change with the config.
TOOL_PREFIXES: tuple[str, ...] = tuple(p for m in KINDS.values() for p in getattr(m, "TOOL_PREFIXES", ()))


def configured(cfg) -> list[tuple[str, ModuleType, dict]]:
    """(name, module, settings) for each plugin in the config."""
    return [(name, KINDS[s["kind"]], s) for name, s in sorted(cfg.plugins.items())]


def sources(cfg) -> list[tuple[str, ModuleType, dict]]:
    """(name, module, settings) for each configured plugin that fills a vault."""
    return [(name, m, s) for name, m, s in configured(cfg) if hasattr(m, "run")]


def every(module: ModuleType, settings: dict) -> int:
    return settings.get("every", module.EVERY)


def due(last: float | None, every: int, now: float) -> bool:
    """A source runs when it never has, or `every` seconds after its last run."""
    return last is None or now - last >= every


OWNER_FILE = ".vl-source"


def owner(vault_path: Path) -> dict | None:
    """Which computer fills a source vault: {"source", "machine", "host"}, or None."""
    try:
        data = json.loads((vault_path / OWNER_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claim(vault_path: Path, vault: str, source: str, take_over: bool) -> None:
    """Only one computer fills a source vault. Two would write their own conversions of
    the same files, and git's union merge would join them into one note.
    """
    me = machine_id()
    found = owner(vault_path)
    if found and found.get("machine") != me and not take_over:
        raise VlError(f"{vault} is filled from {found.get('host') or 'another computer'}. "
                      f"To move it here, run `vl sync {vault} --take-over`.")
    mine = {"source": source, "machine": me, "host": socket.gethostname().split(".")[0]}
    if found != mine:
        (vault_path / OWNER_FILE).write_text(json.dumps(mine) + "\n")


def enabled(runtime: dict) -> list[tuple[str, ModuleType, dict]]:
    """(name, module, data) for each plugin in runtime.json."""
    return [(name, KINDS[p["kind"]], p.get("data") or {}) for name, p in sorted((runtime.get("plugins") or {}).items())]


def plugin_for_tool(runtime: dict, tool: str) -> tuple[ModuleType, dict] | None:
    """The enabled plugin that answers for a tool, and its data."""
    for name, p in sorted((runtime.get("plugins") or {}).items()):
        if tool.startswith(tuple(p.get("tool_prefixes") or ())):
            return KINDS[p["kind"]], p.get("data") or {}
    return None
