"""Everything vaultlines changes in Claude Code's settings.

vaultlines owns only these things, so it can rewrite them safely:
  - MCP servers whose names start with "vl-"
  - permission rules that start with "mcp__vl-"
  - the "basicMemory" block (read by the Basic Memory plugin)
"""

from __future__ import annotations

import os
from pathlib import Path

from .rules import PREFIX, is_managed_rule
from .util import read_json, run, write_json, home

MARKETPLACE = "https://github.com/basicmachines-co/basic-memory.git"
PLUGIN = "basic-memory@basicmachines-co"


def claude_dir() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else home() / ".claude"


def user_settings_path() -> Path:
    return claude_dir() / "settings.json"


def state_path() -> Path:
    """Claude Code's own state file, where user and folder MCP servers live."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return home() / ".claude.json"


def _state() -> dict:
    return read_json(state_path(), {})


def user_servers() -> dict[str, dict]:
    return {k: v for k, v in _state().get("mcpServers", {}).items() if k.startswith(PREFIX)}


def folder_servers() -> dict[str, dict[str, dict]]:
    """Folder -> its vl- servers, for every folder that has any."""
    out = {}
    for folder, project in _state().get("projects", {}).items():
        servers = {k: v for k, v in (project.get("mcpServers") or {}).items() if k.startswith(PREFIX)}
        if servers:
            out[folder] = servers
    return out


def same_server(entry: dict, argv: list[str]) -> bool:
    return [entry.get("command"), *entry.get("args", [])] == argv


def add_server(name: str, argv: list[str], scope: str, cwd: str | None = None) -> None:
    run(["claude", "mcp", "remove", "-s", scope, name], cwd=cwd, check=False)
    run(["claude", "mcp", "add", "-s", scope, name, "--", *argv], cwd=cwd)


def remove_server(name: str, scope: str, cwd: str | None = None) -> None:
    run(["claude", "mcp", "remove", "-s", scope, name], cwd=cwd, check=False)


def install_plugin() -> None:
    run(["claude", "plugin", "marketplace", "add", MARKETPLACE,
         "--sparse", ".claude-plugin", "plugins/claude-code", "--scope", "user"])
    run(["claude", "plugin", "install", PLUGIN, "--scope", "user"])


def plugin_installed() -> bool:
    result = run(["claude", "plugin", "list"], check=False)
    return PLUGIN in result.stdout


def update_settings(
    path: Path,
    primary: str | None,
    reads: list[str] | None = None,
    ask: list[str] | None = None,
    deny: list[str] | None = None,
) -> bool:
    """Replace vaultlines' part of a settings file. Returns True if it changed."""
    before = read_json(path, {})
    data = {k: v for k, v in before.items()}
    perms = dict(data.get("permissions", {}))
    for key, extra in (("allow", []), ("ask", ask or []), ("deny", deny or [])):
        rules = [r for r in perms.get(key, []) if not is_managed_rule(r)] + extra
        if rules:
            perms[key] = rules
        else:
            perms.pop(key, None)
    if perms:
        data["permissions"] = perms
    else:
        data.pop("permissions", None)
    if primary:
        block = {"primaryProject": primary, "captureFolder": "sessions", "captureEvents": False}
        if reads:
            block["secondaryProjects"] = reads
        data["basicMemory"] = block
    else:
        data.pop("basicMemory", None)
    if data == before:
        return False
    if not data and not path.exists():
        return False
    write_json(path, data)
    return True


def git_ignore_local_settings(folder: str) -> None:
    """Keep .claude/settings.local.json out of git, if the folder is a repo."""
    inside = run(["git", "-C", folder, "rev-parse", "--is-inside-work-tree"], check=False)
    if inside.stdout.strip() != "true":
        return
    ignored = run(["git", "-C", folder, "check-ignore", "-q", ".claude/settings.local.json"], check=False)
    if ignored.returncode == 0:
        return
    exclude = run(["git", "-C", folder, "rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    exclude_path = Path(exclude) if os.path.isabs(exclude) else Path(folder) / exclude
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    with exclude_path.open("a") as f:
        f.write("\n.claude/settings.local.json\n")
