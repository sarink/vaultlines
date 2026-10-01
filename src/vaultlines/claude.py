"""Everything vaultlines changes in Claude Code's settings.

vaultlines owns only these things, so it can rewrite them safely:
  - hooks whose command is `vl hook`
  - the "basicMemory" block (read by the Basic Memory plugin) in listed folders
  - the one user-level MCP server named "basic-memory", when the Basic Memory plugin is on
It also removes what vaultlines 0.2 left behind: "vl-*" servers and "mcp__vl-*" rules.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from .util import home, read_json, run, write_json

V2_PREFIX = "vl-"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse")
HOOK_TIMEOUT = 10


def claude_dir() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else home() / ".claude"


def user_settings_path() -> Path:
    return claude_dir() / "settings.json"


def plugin_user_settings_path() -> Path:
    """The Basic Memory plugin always reads ~/.claude/settings.json, whatever CLAUDE_CONFIG_DIR says."""
    return home() / ".claude" / "settings.json"


def folder_settings_path(folder: str) -> Path:
    """Where a listed folder's basicMemory block goes. Your home folder's is the user-level one."""
    if Path(folder) == home().resolve() or Path(folder) == home():
        return plugin_user_settings_path()
    return Path(folder) / ".claude" / "settings.local.json"


def state_path() -> Path:
    """Claude Code's own state file, where user and folder MCP servers live."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return home() / ".claude.json"


def _state() -> dict:
    return read_json(state_path(), {})


# ---------------------------------------------------------------- MCP servers

def user_servers() -> dict[str, dict]:
    return dict(_state().get("mcpServers", {}))


def v2_folder_servers() -> dict[str, list[str]]:
    """Folder -> the vl-* servers vaultlines 0.2 added there."""
    out = {}
    for folder, project in _state().get("projects", {}).items():
        names = [k for k in (project.get("mcpServers") or {}) if k.startswith(V2_PREFIX)]
        if names:
            out[folder] = names
    return out


def same_server(entry: dict, argv: list[str]) -> bool:
    return [entry.get("command"), *entry.get("args", [])] == argv


def add_server(name: str, argv: list[str], scope: str, cwd: str | None = None) -> None:
    run(["claude", "mcp", "remove", "-s", scope, name], cwd=cwd, check=False)
    run(["claude", "mcp", "add", "-s", scope, name, "--", *argv], cwd=cwd)


def remove_server(name: str, scope: str, cwd: str | None = None) -> None:
    run(["claude", "mcp", "remove", "-s", scope, name], cwd=cwd, check=False)


# ---------------------------------------------------------------- settings files

def _is_v2_rule(rule: str) -> bool:
    return rule.startswith(f"mcp__{V2_PREFIX}")


def update_settings(path: Path, block: dict | None) -> bool:
    """Set (or with None, remove) the basicMemory block, and drop vaultlines 0.2's rules.

    Returns True if the file changed.
    """
    before = read_json(path, {})
    data = dict(before)
    perms = dict(data.get("permissions", {}))
    for key in ("allow", "ask", "deny"):
        if key in perms:
            rules = [r for r in perms[key] if not _is_v2_rule(r)]
            if rules:
                perms[key] = rules
            else:
                perms.pop(key)
    if perms:
        data["permissions"] = perms
    else:
        data.pop("permissions", None)
    if block:
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


# ---------------------------------------------------------------- hooks

def vl_command() -> str:
    """The vl that is running now, so hooks call the same install."""
    me = Path(sys.argv[0])
    path = str(me.absolute()) if me.name == "vl" and me.exists() else (shutil.which("vl") or "vl")
    return f"{shlex.quote(path)} hook"


def is_our_hook(hook: dict) -> bool:
    try:
        words = shlex.split(str(hook.get("command", "")))
    except ValueError:
        return False
    return len(words) >= 2 and words[-1] == "hook" and os.path.basename(words[-2]) == "vl"


def _without_ours(hooks: dict) -> dict:
    out = {}
    for event, groups in hooks.items():
        kept = []
        for group in groups if isinstance(groups, list) else []:
            inner = [h for h in group.get("hooks", []) if not is_our_hook(h)]
            if inner:
                kept.append({**group, "hooks": inner})
        if kept:
            out[event] = kept
    return out


def wanted_hooks(command: str) -> dict:
    from .hook import MATCHER

    hook = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
    return {"SessionStart": [{"hooks": [hook]}],
            "UserPromptSubmit": [{"hooks": [hook]}],
            "PreToolUse": [{"matcher": MATCHER, "hooks": [hook]}]}


def install_hooks(path: Path | None = None, command: str | None = None) -> bool:
    """Add vl's hooks to a settings file (default: the user's). Returns True if it changed."""
    path = path or user_settings_path()
    before = read_json(path, {})
    hooks = _without_ours(before.get("hooks", {}))
    for event, groups in wanted_hooks(command or vl_command()).items():
        hooks[event] = hooks.get(event, []) + groups
    data = {**before, "hooks": hooks}
    if data == before:
        return False
    write_json(path, data)
    return True


def remove_hooks(path: Path | None = None) -> bool:
    path = path or user_settings_path()
    before = read_json(path, {})
    if "hooks" not in before:
        return False
    data = dict(before)
    hooks = _without_ours(before["hooks"])
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks")
    if data == before:
        return False
    write_json(path, data)
    return True


def hooks_installed(path: Path | None = None) -> list[str]:
    """The events that have a `vl hook` hook with the right matcher."""
    from .hook import MATCHER

    hooks = read_json(path or user_settings_path(), {}).get("hooks", {})
    found = []
    for event in HOOK_EVENTS:
        for group in hooks.get(event, []) if isinstance(hooks.get(event), list) else []:
            ours = any(is_our_hook(h) for h in group.get("hooks", []))
            if ours and (event != "PreToolUse" or group.get("matcher") == MATCHER):
                found.append(event)
                break
    return found


def disables_hooks(folder: str) -> list[Path]:
    """Settings files that switch off every hook for sessions in `folder`.

    Looks at your user settings, and at each folder from `folder` up to its git
    repo's root (or just `folder` if it isn't in a repo).
    """
    files = [user_settings_path()]
    top = run(["git", "-C", folder, "rev-parse", "--show-toplevel"], check=False).stdout.strip()
    d = Path(folder)
    while True:
        files += [d / ".claude" / "settings.json", d / ".claude" / "settings.local.json"]
        if not top or str(d) == top or os.path.realpath(d) == os.path.realpath(top) or d.parent == d:
            break
        d = d.parent
    out = []
    for f in files:
        try:
            if read_json(f, {}).get("disableAllHooks") is True:
                out.append(f)
        except (OSError, ValueError):
            continue
    return out
