"""The Basic Memory plugin.

One Basic Memory server serves every vault: each vault is a Basic Memory project with
the vault's name. The hook asks `on_call()` which vaults a call touches:

  - The tool name says read or write.
  - `project` names the vault. If it's missing, the hook fills in the folder's
    `writes` vault, so a call never falls through to Basic Memory's default project.
  - A `memory://` link whose first segment is a project routes the call there,
    so that vault counts too.
  - Anything that could reach other projects in a way vl can't check is blocked:
    `project_id`, `workspace`, `search_all_projects`, `recent_activity` with no
    project, the `search` and `fetch` tools, and any argument not listed below.

It also reports the vaults the Basic Memory Claude Code plugin briefs a new session
from (`context_vaults()`).

The hook imports this module, so it only imports the standard library at the top.
Setup (registering projects, the server, the Claude Code plugin) is below.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .api import Access, here

if TYPE_CHECKING:
    from ..config import Config, Folder

SERVER = "basic-memory"
TOOL_PREFIX = f"mcp__{SERVER}__"
TOOL_PREFIXES = (TOOL_PREFIX,)
COMMAND = "uvx basic-memory"
KEYS = {"command"}
MARKETPLACE = "https://github.com/basicmachines-co/basic-memory.git"
PLUGIN = "basic-memory@basicmachines-co"

_PAGES = {"page", "page_number", "page_size", "limit", "per_page", "output_format"}
_SINCE = {"timeframe", "since", "time_range", "lookback"}

# Arguments each tool may take, besides `project` (Basic Memory 0.23.2, with its aliases).
READS: dict[str, set[str]] = {
    "read_note": {"identifier", "include_frontmatter", *_PAGES},
    "view_note": {"identifier"},
    "read_content": {"path", "file_path", "filepath", "file"},
    "build_context": {"url", "uri", "memory_url", "depth", "max_related", "max_results", *_SINCE, *_PAGES},
    "recent_activity": {"type", "types", "kind", "depth", *_SINCE, *_PAGES},
    "search_notes": {"query", "q", "search", "text", "search_type", "note_types", "note_type", "types",
                     "entity_types", "entity_type", "categories", "category", "after_date", "since", "after",
                     "from_date", "metadata_filters", "tags", "status", "min_similarity", "threshold",
                     "similarity_threshold", "search_all_projects", "all_projects", *_PAGES},
    "list_directory": {"dir_name", "directory", "folder", "path", "dir", "depth", "file_name_glob", "glob",
                       "pattern", "filter", "sort", *_PAGES},
    "schema_validate": {"note_type", "identifier", "output_format"},
    "schema_infer": {"note_type", "threshold", "output_format"},
    "schema_diff": {"note_type", "output_format"},
}
WRITES: dict[str, set[str]] = {
    "write_note": {"title", "content", "directory", "folder", "dir", "path", "tags", "note_type", "metadata",
                   "overwrite", "output_format"},
    "edit_note": {"identifier", "operation", "content", "new_content", "replacement", "replace_with", "section",
                  "section_heading", "heading", "find_text", "find", "old_text", "old_content", "search",
                  "expected_replacements", "replace_subsections", "metadata", "output_format"},
    "delete_note": {"identifier", "is_directory", "is_dir", "output_format"},
    "move_note": {"identifier", "destination_path", "dest_path", "new_path", "to", "destination",
                  "destination_folder", "dest_folder", "to_folder", "is_directory", "is_dir", "output_format"},
}
# Tools that don't read notes: they list projects and settings.
NO_VAULT: dict[str, set[str]] = {
    "list_memory_projects": {"output_format"},
    "list_workspaces": {"output_format"},
    "basic_memory_diagnostics": set(),
}
BLOCKED = {
    "create_memory_project": "Vaults are managed with `vl vault`, not through Claude.",
    "delete_project": "Vaults are managed with `vl vault`, not through Claude.",
    "search": "This tool can't be limited to one vault. Use search_notes with project=\"...\".",
    "fetch": "This tool can't be limited to one vault. Use read_note with project=\"...\".",
}
NEEDS_PROJECT = {"recent_activity"}  # without a project, it covers every project


@dataclass
class Resolved:
    """Which Basic Memory projects a call uses."""
    kind: str = "read"  # "read" or "write"
    projects: list[str] = field(default_factory=list)
    block: str | None = None
    updated_input: dict | None = None


def norm(name: str) -> str:
    """Basic Memory matches project names by permalink: lowercase, dashes."""
    return re.sub(r"[\s_]+", "-", name.strip().lower())


def _memory_urls(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip().startswith("memory://") else []
    if isinstance(value, list):
        return [u for v in value for u in _memory_urls(v)]
    if isinstance(value, dict):
        return [u for v in value.values() for u in _memory_urls(v)]
    return []


def _url_project(url: str) -> str | None:
    """The first segment of a memory:// URL, if the URL has more than one."""
    path = url.strip()[len("memory://"):].strip("/")
    if "/" not in path:
        return None
    first, rest = path.split("/", 1)
    return None if not first or not rest or "*" in first else first


def resolve(tool: str, args: dict, projects: list[str], default_project: str | None) -> Resolved:
    """Which projects a call uses. `projects` are all Basic Memory project names.

    `default_project` is the project for the folder's `writes` vault (None if the
    folder has no vaults).
    """
    kind = "write" if tool in WRITES else "read"
    if tool in BLOCKED:
        return Resolved(kind, block=BLOCKED[tool])
    allowed = READS.get(tool) or WRITES.get(tool)
    if allowed is None:
        if tool in NO_VAULT:
            allowed = NO_VAULT[tool]
        else:
            return Resolved(kind, block=f"vl doesn't know the Basic Memory tool '{tool}' yet, so it's blocked.")
    args = dict(args or {})

    for key in ("project_id", "workspace"):
        if args.get(key) is not None:
            return Resolved(kind, block=f"Don't pass `{key}`. Name the vault with project=\"...\".")
    for key in ("search_all_projects", "all_projects"):
        if args.get(key):
            return Resolved(kind, block="Searching every project is off. Search one vault at a time with project=\"...\".")
    unknown = sorted(k for k in args if k not in allowed and k not in ("project", "project_id", "workspace"))
    if unknown:
        return Resolved(kind, block=f"vl doesn't know the argument(s) {', '.join(unknown)} for {tool}, so it's blocked. "
                                  "Pass only the documented arguments.")
    if tool in NO_VAULT:
        return Resolved(kind)

    by_norm = {norm(p): p for p in projects}
    used: list[str] = []
    project = args.get("project")
    updated = None
    if project is None or (isinstance(project, str) and not project.strip()):
        if tool in NEEDS_PROJECT:
            return Resolved(kind, block=f"Pass project=\"...\" to {tool}; without it, it looks at every vault.")
        if default_project is None:
            return Resolved(kind, block="No vaults are set up for this folder.")
        updated = {**args, "project": default_project}
        used.append(default_project)
    elif not isinstance(project, str) or "/" in project.strip().strip("/"):
        return Resolved(kind, block="Pass project as a plain vault name, like project=\"notes\".")
    elif norm(project) in by_norm:
        used.append(by_norm[norm(project)])
    else:
        used.append(project.strip())  # not a Basic Memory project; the hook reports it

    for url in _memory_urls(args):
        first = _url_project(url)
        if first and norm(first) in by_norm and by_norm[norm(first)] not in used:
            used.append(by_norm[norm(first)])
    return Resolved(kind, used, updated_input=updated)


# ---------------------------------------------------------------- the hook's side

def on_call(tool: str, args: dict, folder: dict | None, data: dict) -> Access:
    """Which vaults a call touches. `folder` is the session's folder entry in runtime.json."""
    projects = data["projects"]  # project -> vault, or None for a project that isn't a vault
    default = next((p for p, v in projects.items() if v == folder["writes"]), None) if folder else None
    r = resolve(tool[len(TOOL_PREFIX):], args, list(projects), default)
    if r.block:
        return Access(r.kind, block=r.block)
    vaults = []
    for project in r.projects:
        vault = projects.get(project)
        if vault is None:
            return Access(r.kind, block=f"Basic Memory project '{project}' isn't a vault vl knows. {here(folder)}")
        vaults.append(vault)
    return Access(r.kind, vaults, updated_input=r.updated_input)


def briefing(writes: str, data: dict) -> tuple[str, str]:
    return f'Basic Memory project="{writes}"', 'Always pass project="..." to Basic Memory tools.'


def _settings_block(path: Path) -> tuple[dict | None, bool]:
    """(block, counts) like the plugin: a missing file or key doesn't count; a broken one does."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    if not isinstance(data, dict):
        return None, True
    if "basicMemory" not in data:
        return None, False
    block = data["basicMemory"]
    return (block if isinstance(block, dict) else None), True


def plugin_projects(project_dir: str) -> list[str]:
    """The projects the Basic Memory plugin briefs from, found the way the plugin finds them:
    ~/.claude/settings.json, then the nearest folder with a .claude/settings(.local).json.
    """
    home = Path(os.path.expanduser("~"))
    start = Path(os.path.realpath(project_dir))
    nearest = next((d for d in [start, *start.parents]
                    if (d / ".claude" / "settings.json").is_file()
                    or (d / ".claude" / "settings.local.json").is_file()), start)
    files = [home / ".claude" / "settings.json"]
    if nearest != home:
        files += [nearest / ".claude" / "settings.json", nearest / ".claude" / "settings.local.json"]
    merged: dict = {}
    for f in files:
        block, counts = _settings_block(f)
        if counts and block is None:
            return []  # the plugin does nothing when a settings file is broken
        if block:
            merged.update(block)
    out = []
    primary = merged.get("primaryProject")
    if isinstance(primary, str) and primary.strip():
        out.append(primary.strip())
    secondary = merged.get("secondaryProjects") if isinstance(merged.get("secondaryProjects"), list) else []
    team = merged.get("teamProjects") if isinstance(merged.get("teamProjects"), dict) else {}
    for ref in [*secondary, *team]:
        if isinstance(ref, str) and ref.strip() and ref.strip() not in out:
            out.append(ref.strip())
    return out


def context_vaults(project_dir: str, data: dict) -> list[str]:
    """The vaults the Basic Memory Claude Code plugin briefs a new session from."""
    if not data.get("plugin"):
        return []
    by_norm = {norm(p): v for p, v in data["projects"].items()}
    return [by_norm[norm(p)] for p in plugin_projects(project_dir) if by_norm.get(norm(p))]


# ---------------------------------------------------------------- setup

def validate(settings: dict) -> tuple[str, str] | None:
    command = settings.get("command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        return "command", 'should be a command, like "uvx basic-memory"'
    return None


def command(settings: dict) -> str:
    return settings.get("command", COMMAND)


def _cmd(settings: dict) -> list[str]:
    return shlex.split(command(settings))


def mcp_argv(settings: dict) -> list[str]:
    return [*_cmd(settings), "mcp"]


def projects(settings: dict) -> dict[str, Path]:
    from ..util import VlError, run

    out = run([*_cmd(settings), "tool", "list-projects"]).stdout
    start = out.find("{")
    if start < 0:
        raise VlError(f"Couldn't read Basic Memory projects:\n{out}")
    data = json.JSONDecoder().raw_decode(out[start:])[0]
    return {p["name"]: Path(p["path"]).resolve() for p in data.get("projects", [])}


def ensure_project(settings: dict, name: str, path: Path, current: dict[str, Path] | None = None) -> None:
    """Register a vault as a Basic Memory project with the vault's name."""
    from ..util import VlError, run

    path = path.resolve()
    current = projects(settings) if current is None else current
    if current.get(name) == path:
        return
    if name in current:
        raise VlError(
            f"Basic Memory already has a project '{name}' at {current[name]}, not {path}. "
            f"Remove it with `{command(settings)} project remove {name}` or pick another name."
        )
    for other, other_path in list(current.items()):
        if other_path == path:
            # Same folder under another name: re-register it under the vault name.
            run([*_cmd(settings), "project", "remove", other])
            del current[other]
    run([*_cmd(settings), "project", "add", name, str(path)])
    current[name] = path


def remove_project(settings: dict, name: str) -> None:
    from ..util import run

    if name in projects(settings):
        run([*_cmd(settings), "project", "remove", name])


def set_default(settings: dict, name: str) -> None:
    from ..util import run

    run([*_cmd(settings), "project", "default", name])


def configure(settings: dict) -> None:
    """Stop Basic Memory from rewriting notes that other people also edit."""
    from ..util import run

    run([*_cmd(settings), "config", "set", "disable_permalinks", "true"])
    run([*_cmd(settings), "config", "set", "ensure_frontmatter_on_sync", "false"])


def setting(settings: dict, key: str) -> str:
    from ..util import run

    return run([*_cmd(settings), "config", "get", key], check=False).stdout.strip()


def install_plugin() -> None:
    from ..util import run

    run(["claude", "plugin", "marketplace", "add", MARKETPLACE,
         "--sparse", ".claude-plugin", "plugins/claude-code", "--scope", "user"])
    run(["claude", "plugin", "install", PLUGIN, "--scope", "user"])


def plugin_installed() -> bool:
    from ..util import run

    return PLUGIN in run(["claude", "plugin", "list"], check=False).stdout


def plugin_block(project: str) -> dict:
    """The Basic Memory plugin's settings for a folder that writes to `project`.

    Checkpoints go to sessions/, which vl keeps out of git, so they stay on this computer.
    """
    return {"primaryProject": project, "captureFolder": "sessions", "captureEvents": False}


def plugin_targets(cfg: Config) -> dict[str, Folder]:
    """Settings file -> the folder whose writes vault the Basic Memory plugin should use there.

    Folders that hold your home folder ("~", "/") are covered by the user-level block,
    which the plugin reads everywhere a closer settings file doesn't override it.
    """
    from .. import claude
    from ..config import folder_for
    from ..util import home

    me = str(home().resolve())
    targets = {}
    top = folder_for(cfg, me)
    if top:
        targets[str(claude.plugin_user_settings_path())] = top
    for f in cfg.listed():
        if not (f.path == me or me.startswith(f.path.rstrip("/") + "/")):
            targets[str(claude.folder_settings_path(f.path))] = f
    return targets


def _plugin_blocks(wanted: dict[str, Folder], before: list[str], warnings: list[str]) -> list[str]:
    """Point the Basic Memory plugin at each listed folder's writes vault; remove old blocks.

    Returns the settings files that now hold a block.
    """
    from .. import claude
    from ..util import contract

    for old in before:
        if old not in wanted and Path(old).exists():
            claude.update_settings(Path(old), None)
    for path, f in wanted.items():
        if not Path(f.path).is_dir():
            warnings.append(f"folder {contract(f.path)} is missing")
            continue
        claude.update_settings(Path(path), plugin_block(f.writes))
        if path != str(claude.plugin_user_settings_path()):
            claude.git_ignore_local_settings(f.path)
    return sorted(wanted)


def init(cfg: Config, settings: dict) -> None:
    from ..util import say

    say("Configuring Basic Memory")
    configure(settings)
    if not plugin_installed():
        say("Installing the Basic Memory plugin for Claude Code")
        install_plugin()


def apply(cfg: Config, settings: dict, state: dict, warnings: list[str]) -> dict:
    """Register every vault as a project, add the one server, and point the plugin at each folder."""
    from .. import claude
    from ..config import folder_for
    from ..util import home

    current = projects(settings)
    for v in cfg.vaults.values():
        if v.path.exists():
            ensure_project(settings, v.name, v.path, current)
    argv = mcp_argv(settings)
    server = claude.user_servers().get(SERVER)
    if server is None or not claude.same_server(server, argv):
        claude.add_server(SERVER, argv, "user")
    top = folder_for(cfg, home())
    if top and top.writes in current:
        set_default(settings, top.writes)
    return {"blocks": _plugin_blocks(plugin_targets(cfg), state.get("blocks", []), warnings)}


def off(state: dict, warnings: list[str]) -> None:
    _plugin_blocks({}, state.get("blocks", []), warnings)


def data(cfg: Config, settings: dict) -> dict:
    by_path = {str(v.path): name for name, v in cfg.vaults.items()}
    return {"plugin": plugin_installed(),
            "projects": {p: by_path.get(str(folder)) for p, folder in sorted(projects(settings).items())}}


def vault_removed(cfg: Config, settings: dict, name: str) -> None:
    remove_project(settings, name)


def doctor(cfg: Config, settings: dict, check) -> None:
    import shutil

    from .. import claude
    from ..util import contract, read_json, say

    say("Basic Memory")
    tool = _cmd(settings)[0]
    found = shutil.which(tool)
    check(bool(found), f"{tool}: {found or 'not found'}")
    check(setting(settings, "disable_permalinks").lower().endswith("true"), "permalinks off", "vl init")
    current = projects(settings)
    for v in cfg.vaults.values():
        check(current.get(v.name) == v.path, f"vault '{v.name}' is a Basic Memory project", "vl apply")
    server = claude.user_servers().get(SERVER)
    check(server is not None and claude.same_server(server, mcp_argv(settings)),
          f"one '{SERVER}' server for every vault", "vl apply")
    check(plugin_installed(), "Basic Memory plugin installed", "vl init")
    for path, f in plugin_targets(cfg).items():
        block = read_json(Path(path), {}).get("basicMemory") or {}
        check(block.get("primaryProject") == f.writes, f"{contract(path)}: plugin writes to {f.writes}", "vl apply")
