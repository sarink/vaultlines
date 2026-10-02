"""Plugins: built-in extras that report facts to vl. vl's core decides about access.

There are two kinds, each a module where every function is optional:

  KINDS     plugins that answer for tool calls, like Basic Memory. config.toml turns them
            on and off (basic_memory = false).
  SOURCES   what fills a vault from elsewhere, named by `kind` in the vault's
            vault.toml [source] table, like gdrive. A filled vault is read-only.

The hook side is pure, fast and uses only the standard library, because `vl hook`
imports it. `data` is what the plugin put in runtime.json; `rules` is the session's
{"writes": short name, "reads": [...]}:

  TOOL_PREFIXES                           tool names the plugin answers for
  on_call(tool, args, rules, data)        -> Access: which vaults a call touches
  context_vaults(project_dir, data)       -> vaults already put into a new session
  briefing(writes, data)                  -> (text inside the parentheses, sentence at the end)
                                             of the SessionStart message
  session_start(rules, runtime, data)     set things up for a new session (Basic Memory:
                                          its settings block in the repo)
  source_briefing(short, vault_id)        (sources) a sentence for the SessionStart message

The vl side imports what it needs inside each function. `settings` is the plugin's
settings, with `kind`:

  init(cfg, settings)                     one-time setup, from `vl init`
  apply(cfg, settings, state, warnings)   -> new state: make the world match. `state` is
                                             what the last apply returned.
  off(state, warnings)                    undo apply, after the plugin was turned off
  data(cfg, settings)                     -> the plugin's part of runtime.json
  doctor(cfg, settings, check)            print a section of `vl doctor`
  commands(subparsers)                    add a `vl NAME ...` command group
  joined(cfg, owner)                      (sources) after `vl org join OWNER`
"""

from __future__ import annotations

from types import ModuleType

from . import basic_memory, gdrive

KINDS: dict[str, ModuleType] = {"basic-memory": basic_memory}
SOURCES: dict[str, ModuleType] = {"gdrive": gdrive}

# Every built-in prefix, on or off, so the hook's matcher doesn't change with the config.
TOOL_PREFIXES: tuple[str, ...] = tuple(p for m in KINDS.values() for p in getattr(m, "TOOL_PREFIXES", ()))


def configured(cfg) -> list[tuple[str, ModuleType, dict]]:
    """(name, module, settings) for each plugin that is on."""
    return [("basic-memory", basic_memory, {"kind": "basic-memory"})] if cfg.basic_memory else []


def modules() -> list[ModuleType]:
    return [*KINDS.values(), *SOURCES.values()]


def add_commands(subparsers) -> None:
    for module in modules():
        if hasattr(module, "commands"):
            module.commands(subparsers)


def enabled(runtime: dict) -> list[tuple[str, ModuleType, dict]]:
    """(name, module, data) for each plugin in runtime.json."""
    return [(name, KINDS[p["kind"]], p.get("data") or {}) for name, p in sorted((runtime.get("plugins") or {}).items())
            if p.get("kind") in KINDS]


def plugin_for_tool(runtime: dict, tool: str) -> tuple[ModuleType, dict] | None:
    """The enabled plugin that answers for a tool, and its data."""
    for name, p in sorted((runtime.get("plugins") or {}).items()):
        if tool.startswith(tuple(p.get("tool_prefixes") or ())) and p.get("kind") in KINDS:
            return KINDS[p["kind"]], p.get("data") or {}
    return None
