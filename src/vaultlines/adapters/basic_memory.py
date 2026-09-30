"""The Basic Memory adapter.

One Basic Memory server serves every vault: each vault is a Basic Memory project with
the vault's name. The hook uses `resolve()` to learn which vaults a call touches:

  - The tool name says read or write.
  - `project` names the vault. If it's missing, the hook fills in the folder's
    `writes` vault, so a call never falls through to Basic Memory's default project.
  - A `memory://` link whose first segment is a project routes the call there,
    so that vault counts too.
  - Anything that could reach other projects in a way vl can't check is blocked:
    `project_id`, `workspace`, `search_all_projects`, `recent_activity` with no
    project, the `search` and `fetch` tools, and any argument not listed below.

`resolve()` and the tables are imported by the hook, so this module only imports the
standard library at the top. Setup (registering projects, the server) is below.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

SERVER = "basic-memory"
TOOL_PREFIX = f"mcp__{SERVER}__"

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
class Access:
    """Which vaults a Basic Memory call touches."""
    kind: str = "read"  # "read" or "write"
    projects: list[str] = field(default_factory=list)  # Basic Memory project names
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


def resolve(tool: str, args: dict, projects: list[str], default_project: str | None) -> Access:
    """Which projects a call uses. `projects` are all Basic Memory project names.

    `default_project` is the project for the folder's `writes` vault (None if the
    folder has no vaults).
    """
    kind = "write" if tool in WRITES else "read"
    if tool in BLOCKED:
        return Access(kind, block=BLOCKED[tool])
    allowed = READS.get(tool) or WRITES.get(tool)
    if allowed is None:
        if tool in NO_VAULT:
            allowed = NO_VAULT[tool]
        else:
            return Access(kind, block=f"vl doesn't know the Basic Memory tool '{tool}' yet, so it's blocked.")
    args = dict(args or {})

    for key in ("project_id", "workspace"):
        if args.get(key) is not None:
            return Access(kind, block=f"Don't pass `{key}`. Name the vault with project=\"...\".")
    for key in ("search_all_projects", "all_projects"):
        if args.get(key):
            return Access(kind, block="Searching every project is off. Search one vault at a time with project=\"...\".")
    unknown = sorted(k for k in args if k not in allowed and k not in ("project", "project_id", "workspace"))
    if unknown:
        return Access(kind, block=f"vl doesn't know the argument(s) {', '.join(unknown)} for {tool}, so it's blocked. "
                                  "Pass only the documented arguments.")
    if tool in NO_VAULT:
        return Access(kind)

    by_norm = {norm(p): p for p in projects}
    used: list[str] = []
    project = args.get("project")
    updated = None
    if project is None or (isinstance(project, str) and not project.strip()):
        if tool in NEEDS_PROJECT:
            return Access(kind, block=f"Pass project=\"...\" to {tool}; without it, it looks at every vault.")
        if default_project is None:
            return Access(kind, block="No vaults are set up for this folder.")
        updated = {**args, "project": default_project}
        used.append(default_project)
    elif not isinstance(project, str) or "/" in project.strip().strip("/"):
        return Access(kind, block="Pass project as a plain vault name, like project=\"notes\".")
    elif norm(project) in by_norm:
        used.append(by_norm[norm(project)])
    else:
        used.append(project.strip())  # not a Basic Memory project; the hook reports it

    for url in _memory_urls(args):
        first = _url_project(url)
        if first and norm(first) in by_norm and by_norm[norm(first)] not in used:
            used.append(by_norm[norm(first)])
    return Access(kind, used, updated_input=updated)


# ---------------------------------------------------------------- setup

def _cmd(cfg: Config) -> list[str]:
    return shlex.split(cfg.bm_command)


def mcp_argv(cfg: Config) -> list[str]:
    return [*_cmd(cfg), "mcp"]


def projects(cfg: Config) -> dict[str, Path]:
    from ..util import VlError, run

    out = run([*_cmd(cfg), "tool", "list-projects"]).stdout
    start = out.find("{")
    if start < 0:
        raise VlError(f"Couldn't read Basic Memory projects:\n{out}")
    data = json.JSONDecoder().raw_decode(out[start:])[0]
    return {p["name"]: Path(p["path"]).resolve() for p in data.get("projects", [])}


def ensure_project(cfg: Config, name: str, path: Path, current: dict[str, Path] | None = None) -> None:
    """Register a vault as a Basic Memory project with the vault's name."""
    from ..util import VlError, run

    path = path.resolve()
    current = projects(cfg) if current is None else current
    if current.get(name) == path:
        return
    if name in current:
        raise VlError(
            f"Basic Memory already has a project '{name}' at {current[name]}, not {path}. "
            f"Remove it with `{cfg.bm_command} project remove {name}` or pick another name."
        )
    for other, other_path in list(current.items()):
        if other_path == path:
            # Same folder under another name: re-register it under the vault name.
            run([*_cmd(cfg), "project", "remove", other])
            del current[other]
    run([*_cmd(cfg), "project", "add", name, str(path)])
    current[name] = path


def remove_project(cfg: Config, name: str) -> None:
    from ..util import run

    if name in projects(cfg):
        run([*_cmd(cfg), "project", "remove", name])


def set_default(cfg: Config, name: str) -> None:
    from ..util import run

    run([*_cmd(cfg), "project", "default", name])


def configure(cfg: Config) -> None:
    """Stop Basic Memory from rewriting notes that other people also edit."""
    from ..util import run

    run([*_cmd(cfg), "config", "set", "disable_permalinks", "true"])
    run([*_cmd(cfg), "config", "set", "ensure_frontmatter_on_sync", "false"])


def setting(cfg: Config, key: str) -> str:
    from ..util import run

    return run([*_cmd(cfg), "config", "get", key], check=False).stdout.strip()


def plugin_block(project: str) -> dict:
    """The Basic Memory plugin's settings for a folder that writes to `project`.

    Checkpoints go to sessions/, which vl keeps out of git, so they stay on this computer.
    """
    return {"primaryProject": project, "captureFolder": "sessions", "captureEvents": False}
