"""Plugins: built-in extras that report facts to vl. vl's core decides about access.

A plugin is a module. Every function is optional.

The hook side is pure, fast and uses only the standard library, because `vl hook`
imports it. `data` is what the plugin put in runtime.json:

  TOOL_PREFIXES                           tool names the plugin answers for
  on_call(tool, args, folder, data)       -> Access: which vaults a call touches
  context_vaults(project_dir, data)       -> vaults already put into a new session
  briefing(writes, data)                  -> (text inside the parentheses, sentence at the end)
                                             of the SessionStart message

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
"""

from __future__ import annotations

from types import ModuleType

from . import basic_memory

KINDS: dict[str, ModuleType] = {"basic-memory": basic_memory}

# Every built-in prefix, on or off, so the hook's matcher doesn't change with the config.
TOOL_PREFIXES: tuple[str, ...] = tuple(p for m in KINDS.values() for p in getattr(m, "TOOL_PREFIXES", ()))


def configured(cfg) -> list[tuple[str, ModuleType, dict]]:
    """(name, module, settings) for each plugin in the config."""
    return [(name, KINDS[s["kind"]], s) for name, s in sorted(cfg.plugins.items())]


def enabled(runtime: dict) -> list[tuple[str, ModuleType, dict]]:
    """(name, module, data) for each plugin in runtime.json."""
    return [(name, KINDS[p["kind"]], p.get("data") or {}) for name, p in sorted((runtime.get("plugins") or {}).items())]


def plugin_for_tool(runtime: dict, tool: str) -> tuple[ModuleType, dict] | None:
    """The enabled plugin that answers for a tool, and its data."""
    for name, p in sorted((runtime.get("plugins") or {}).items()):
        if tool.startswith(tuple(p.get("tool_prefixes") or ())):
            return KINDS[p["kind"]], p.get("data") or {}
    return None
