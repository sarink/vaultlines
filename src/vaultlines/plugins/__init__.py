"""Plugins: built-in extras that report facts to vl. vl's core decides about access.

There are two kinds, each a module where every function is optional unless noted:

  KINDS     plugins that answer for tool calls, like Basic Memory. config.toml turns them
            on and off (basic_memory = false).
  SOURCES   source kinds: where a vault's notes come from, named by `kind` in the vault's
            [source] table, like gdrive. A vault with a source is read-only in sessions,
            and refreshed by `vl source refresh`: every hour by its refresh job on GitHub,
            or on any computer.

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

The vl side imports what it needs inside each function. For a plugin, `settings` is its
settings, with `kind`:

  init(cfg, settings)                     one-time setup, from `vl init`
  apply(cfg, settings, state, warnings)   -> new state: make the world match. `state` is
                                             what the last apply returned.
  off(state, warnings)                    undo apply, after the plugin was turned off
  data(cfg, settings)                     -> the plugin's part of runtime.json
  doctor(cfg, settings, check)            print a section of `vl doctor`

A source kind. `source` is the vault's [source] table, already checked:

  NAME                                    what it's called, like "Google Drive" (required)
  OPTIONS                                 {key: help} for the [source] keys besides `kind`;
                                          `vl vault create --source KIND` takes each as --KEY
                                          (required)
  DEFAULTS                                {key: value} for keys that may be left out
  REQUIRED                                keys that must be set. `vl vault create` asks for
                                          the ones left out, at a terminal
  LATER                                   required keys that create() asks for itself, like
                                          gdrive's shared_drive, picked after the login
  GUIDE                                   what you need before `vl vault create --source KIND`,
                                          and how to get it. Shown by --help, and when a
                                          required key is left out
  validate_source(source)                 -> ["key: problem", ...] (required)
  create(vault_id, source, ask)           -> (source, secret): on an admin's computer, before
                                             the vault's repo exists. `secret` is what the
                                             refresh job logs in with. ask(question, choices=None)
                                             asks the person for LATER keys (required)
  SETUP_STEPS                             the refresh job's steps before the refresh (YAML)
  A refresh has two halves, so the code that converts files never runs with the login:
  fetch_changes(root, source, vault_id, secret, force, staged)
                                          -> a status. Log in with secret() (the login's token),
                                             and download what changed into the folder `staged`.
                                             Reads `root`, changes nothing (required)
  convert(root, source, vault_id, staged) -> a status. Make the notes in `root` match the source,
                                             from what's in `staged`. No login (required)
  default_about(source), comments(source) for the vault.toml vl writes
  fetch(vault, source, short, path)       -> the local copy of one original
  login(source), saved_login(source)     your own login on this computer (saved_login -> its
                                          token, or None), for fetching, and for refreshing
                                          when VL_SOURCE_TOKEN isn't set
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
