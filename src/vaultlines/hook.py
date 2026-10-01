"""`vl hook`: the Claude Code hook that guards vaults.

Claude Code runs it on SessionStart and before matching tool calls (PreToolUse).
It reads runtime.json (written by `vl apply` and the daily check), never the
TOML config and never GitHub, so it stays fast.

The rules, for a session in folder F (the closest listed parent of where it started):

1. Focus: a call touching a vault that isn't F's `writes` or `reads` is blocked.
2. Label: every vault read narrows the session label to the people who can see it.
3. Writes: a write to vault V asks (or blocks, with on_leak = "block") if people who
   can see V couldn't see everything the session read.
4. Writes to a `reads` vault always ask.
5. Reads are recorded here, before the call runs.
6. vl itself: writes to its records are blocked; changes to its config, its hooks,
   or `vl` commands that change what it allows ask, unless your latest message
   mentions vl (UserPromptSubmit records that).

`decide()` is pure. `main()` does the I/O. Only the standard library is imported,
plus label.py and the plugins' hook side (see plugins/__init__.py).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import label as lbl
from . import plugins
from .plugins.api import here as _here
from .util import closest_parent, inside, state_dir

READ_TOOLS = {"Read": "file_path"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SEARCH_TOOLS = ("Grep", "Glob")
MATCHER = "^(" + "|".join(["Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob", "Bash",
                            *(f"{p}.*" for p in plugins.TOOL_PREFIXES)]) + ")$"
ERROR = "vl hook error: run `vl doctor`."
RUNTIME_VERSION = 2  # runtime.json's layout


class Blocked(Exception):
    pass


# ---------------------------------------------------------------- paths

def _abs(path: str, cwd: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(path)


def _vault_paths(runtime: dict) -> dict[str, str]:
    """Vault folder -> vault name."""
    return {p: name for name, v in runtime["vaults"].items() for p in v["paths"]}


def vault_of(path: str, runtime: dict) -> str | None:
    folders = _vault_paths(runtime)
    key = closest_parent(folders, path)
    return folders[key] if key else None


def vaults_within(root: str, runtime: dict) -> list[str]:
    """Vaults in `root`, or the vault `root` is in."""
    found = [name for p, name in _vault_paths(runtime).items() if inside(p, root)]
    own = vault_of(root, runtime)
    return list(dict.fromkeys(([own] if own else []) + sorted(found)))


def _glob_root(pattern: str) -> str | None:
    """The fixed part of an absolute Glob pattern, e.g. ~/Vaults/x/**/*.md -> ~/Vaults/x."""
    if not pattern.startswith(("/", "~")):
        return None
    fixed = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
    return fixed if fixed.endswith("/") or fixed == pattern else os.path.dirname(fixed) or "/"


def _home() -> str:
    return os.path.expanduser("~")


def bash_vaults(command: str, cwd: str, runtime: dict) -> list[str]:
    """Vaults a shell command mentions: by absolute path, ~/..., $HOME/..., or relative to cwd.

    Best effort: a command can always build a path in ways no scan can see.
    """
    found = []
    here = vault_of(os.path.realpath(cwd), runtime) if cwd else None
    if here:
        found.append(here)
    homes = {_home(), os.path.realpath(_home())}
    cwd_real = os.path.realpath(cwd) if cwd else None
    for p, name in _vault_paths(runtime).items():
        if name in found:
            continue
        forms = [p]
        for h in homes:
            if inside(p, h) and p != h:
                rel = p[len(h) + 1:]
                forms += [f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"]
        if cwd_real and inside(p, cwd_real) and p != cwd_real:
            rel = p[len(cwd_real) + 1:]
            forms += [rel, f"./{rel}"]
        for form in forms:
            if re.search(r"(?<![\w.~/$-])" + re.escape(form) + r"(?![\w.-])", command):
                found.append(name)
                break
    return found


# ---------------------------------------------------------------- what a call touches

class Call:
    """The vaults one tool call reads and writes."""

    def __init__(self):
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.bash = False
        self.updated_input: dict | None = None
        self.search_root: str | None = None  # set when a Grep/Glob root holds several vaults

    def add(self, vault: str, kind: str) -> None:
        target = self.writes if kind == "write" else self.reads
        if vault not in target:
            target.append(vault)

    @property
    def vaults(self) -> list[str]:
        return list(dict.fromkeys(self.reads + self.writes))


def touched(event: dict, runtime: dict, folder: dict | None) -> Call:
    """Work out which vaults a PreToolUse call touches. Raises Blocked for calls vl refuses."""
    tool = event.get("tool_name") or ""
    args = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    call = Call()

    if tool in READ_TOOLS or tool in WRITE_TOOLS:
        kind = "read" if tool in READ_TOOLS else "write"
        path = args.get(READ_TOOLS.get(tool) or WRITE_TOOLS[tool])
        if isinstance(path, str) and path:
            v = vault_of(_abs(path, cwd), runtime)
            if v:
                call.add(v, kind)
    elif tool in SEARCH_TOOLS:
        # An absolute Glob pattern ignores the folder it runs in.
        pattern_root = _glob_root(args["pattern"]) if tool == "Glob" and isinstance(args.get("pattern"), str) else None
        roots = [pattern_root or args.get("path") or cwd]
        for root in roots:
            if not isinstance(root, str):
                continue
            found = vaults_within(_abs(root, cwd), runtime)
            if len(found) > 1 or (found and vault_of(_abs(root, cwd), runtime) is None):
                call.search_root = root
            for v in found:
                call.add(v, "read")
    elif tool == "Bash":
        command = args.get("command")
        if isinstance(command, str):
            for v in bash_vaults(command, cwd, runtime):
                call.add(v, "read")
                call.add(v, "write")
                call.bash = True
    elif found := plugins.plugin_for_tool(runtime, tool):
        plugin, data = found
        access = plugin.on_call(tool, args, folder, data)
        if access.block:
            raise Blocked(access.block)
        for vault in access.vaults:
            call.add(vault, access.kind)
        call.updated_input = access.updated_input
    return call


# ---------------------------------------------------------------- decide

def _output(event_name: str, **fields) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event_name, **fields}}


def _pre(decision: str | None, reason: str = "", updated: dict | None = None, prefix: str = "vl: ") -> dict | None:
    fields: dict = {}
    if decision:
        fields["permissionDecision"] = decision
        fields["permissionDecisionReason"] = prefix + reason
    if updated is not None:
        fields["updatedInput"] = updated
    return _output("PreToolUse", **fields) if fields else None


def folder_entry(runtime: dict, project_dir: str) -> tuple[str | None, dict | None]:
    folders = runtime.get("folders", {})
    key = closest_parent(folders, os.path.realpath(project_dir)) if project_dir else None
    return key, (folders[key] if key else None)


def _audience(runtime: dict, vault: str) -> dict:
    return runtime["vaults"].get(vault, {}).get("audience") or {"kind": "unknown", "reason": "not in runtime.json"}


def _session_start(event: dict, runtime: dict, state: dict | None, project_dir: str, briefing: list[str]):
    source = event.get("source") or "startup"
    key, _ = folder_entry(runtime, project_dir)
    if state is None or source in ("startup", "clear"):
        if source in ("startup", "clear"):
            state = lbl.new_session(key, lbl.EVERYONE, source)
        else:
            # A fork, or a resume/compact vl has no record of: the context may hold anything.
            state = lbl.new_session(key, lbl.ONLY_YOU, source)
            state["why"] = "it was forked" if source == "fork" else f"vl has no record of it before this {source}"
    else:
        state = dict(state)
    # Plugins (like Basic Memory's briefing) put notes from these vaults into the session.
    label = lbl.from_json(state["label"])
    read = list(state.get("read", []))
    for vault in briefing:
        label = lbl.narrow(label, _audience(runtime, vault))
        if vault not in read:
            read.append(vault)
    state.update(label=lbl.to_json(label), read=read)

    folder = runtime.get("folders", {}).get(state.get("folder") or "")  # a resumed session keeps its folder
    if not folder:
        return None, state
    w = folder["writes"]
    reads = folder.get("reads") or []
    where = runtime["vaults"].get(w, {}).get("show", "")
    inline, extra = [], []
    for _, plugin, data in plugins.enabled(runtime):
        if hasattr(plugin, "briefing"):
            i, e = plugin.briefing(w, data)
            inline.append(i)
            extra.append(e)
    lines = [f"vaultlines: save notes from this folder to the `{w}` vault ({', '.join([*inline, f'folder {where}'])})."]
    if reads:
        lines.append(f"You can also read: {', '.join(reads)}. Writing to those asks first.")
    lines.append(" ".join(["Other vaults are blocked here.", *extra]))
    return _output("SessionStart", additionalContext=" ".join(lines)), state


def _leak_reason(state: dict, runtime: dict, target: str, people: list[str], extra_reads: list[str]) -> str:
    me = runtime.get("me", "")
    read = list(dict.fromkeys(list(state.get("read", [])) + extra_reads))
    target_aud = _audience(runtime, target)
    culprits = [r for r in read if r != target and lbl.new_people(lbl.narrow(None, _audience(runtime, r)), target_aud, me)]
    if culprits:
        first = f"This session read {', '.join(culprits)}."
    elif state.get("why"):
        first = f"This session counts as private because {state['why']}."
    else:
        first = "This session read vaults fewer people can see."
    verb = "would" if len(people) == 1 else "would all"
    return f"{first} {lbl.names(people)} {verb} see this in {target}."


# ---------------------------------------------------------------- vl's own files

VL_COMMAND_RE = re.compile(r"(?:^|[\s;&|(`])(?:\S*/)?vl\s+(init|apply|uninstall|folder|vault)\b")


def _config_dir(runtime: dict) -> str:
    config = runtime.get("config")
    if config:
        return os.path.realpath(os.path.dirname(config))
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")
    return os.path.realpath(os.path.join(base, "vaultlines"))


def _has_vl_hook(data) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return False
    return any(re.search(r"(?:^|/|\s)vl['\"]?\s+hook\s*$", str(h.get("command", "")))
               for groups in data["hooks"].values() if isinstance(groups, list)
               for g in groups if isinstance(g, dict)
               for h in g.get("hooks", []) if isinstance(h, dict))


def _settings_change(path: str, tool: str, args: dict) -> str | None:
    """Why an edit to a Claude Code settings file would switch vl off, or None."""
    if os.path.basename(path) not in ("settings.json", "settings.local.json") \
            or os.path.basename(os.path.dirname(path)) != ".claude":
        return None
    if tool == "Write":
        try:
            new = json.loads(args.get("content") or "")
        except ValueError:
            new = None
        try:
            old = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            old = None
        if isinstance(new, dict) and new.get("disableAllHooks") is True:
            return "It turns on disableAllHooks, which switches vl off."
        if _has_vl_hook(old) and not _has_vl_hook(new):
            return "It removes vl's hooks."
        return None
    edits = args.get("edits") if tool == "MultiEdit" else [args]
    for e in edits if isinstance(edits, list) else []:
        old, new = str(e.get("old_string", "")), str(e.get("new_string", ""))
        if "disableAllHooks" in new:
            return "It changes disableAllHooks, which can switch vl off."
        if re.search(r"vl['\"]?\s+hook", old) and not re.search(r"vl['\"]?\s+hook", new):
            return "It removes vl's hooks."
    return None


def self_guard(event: dict, runtime: dict) -> tuple[str, str] | None:
    """Stop Claude from quietly loosening vl itself: its config, its records, its hooks."""
    tool = event.get("tool_name") or ""
    args = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    state = os.path.realpath(str(state_dir()))
    config = _config_dir(runtime)
    if tool in WRITE_TOOLS:
        path = args.get(WRITE_TOOLS[tool])
        if not isinstance(path, str) or not path:
            return None
        p = _abs(path, cwd)
        if inside(p, state):
            return "deny", "vl's session records and hook data can only be changed by vl."
        if inside(p, config):
            return "ask", "This edits vl's config. Changes take effect after `vl apply`."
        why = _settings_change(p, tool, args)
        return ("ask", f"This Claude Code settings change affects vl. {why}") if why else None
    if tool == "Bash" and isinstance(args.get("command"), str):
        command = args["command"]
        homes = {_home(), os.path.realpath(_home())}
        for d in (state, config):
            forms = [d] + [f"{pre}/{d[len(h) + 1:]}" for h in homes if inside(d, h) and d != h
                           for pre in ("~", "$HOME", "${HOME}")]
            if any(f in command for f in forms):
                return "ask", "This command touches vl's own files (its config or session records)."
        m = VL_COMMAND_RE.search(command)
        if m:
            return "ask", f"This runs `vl {m.group(1)}`, which changes what vl allows. (Mention vl in your message to skip this question.)"
        if "disableAllHooks" in command:
            return "ask", "This command mentions disableAllHooks, which can switch vl off."
    return None


ASKED_VL_RE = re.compile(r"\b(vl|vaultlines)\b", re.IGNORECASE)


def _unknown_session(runtime: dict, project_dir: str) -> dict:
    key, _ = folder_entry(runtime, project_dir)
    state = lbl.new_session(key, lbl.ONLY_YOU, "unknown")
    state["why"] = "vl has no record of how it started"
    return state


def _prompt(event: dict, runtime: dict, state: dict | None, project_dir: str) -> dict:
    """Remember whether your latest message is about vl. Only you can set this: it comes
    from what you type, and Claude can't write to the session record."""
    state = dict(state) if state is not None else _unknown_session(runtime, project_dir)
    state["asked_vl"] = bool(ASKED_VL_RE.search(str(event.get("prompt") or "")))
    return state


def _pre_tool(event: dict, runtime: dict, state: dict | None, project_dir: str):
    guard = self_guard(event, runtime)
    if guard and guard[0] == "deny":
        return _pre("deny", guard[1]), None
    if guard and state and state.get("asked_vl"):
        guard = None  # you asked for this in your latest message
    out, new_state = _vault_rules(event, runtime, state, project_dir)
    if not guard:
        return out, new_state
    fields = (out or {}).get("hookSpecificOutput", {})
    if fields.get("permissionDecision") == "deny":
        return out, new_state
    reason = " ".join(filter(None, [guard[1], fields.get("permissionDecisionReason", "").removeprefix("vl: ")]))
    return _pre("ask", reason, fields.get("updatedInput")), new_state


def _vault_rules(event: dict, runtime: dict, state: dict | None, project_dir: str):
    if state is None:
        state = _unknown_session(runtime, project_dir)
    folders = runtime.get("folders", {})
    folder = folders.get(state.get("folder")) if state.get("folder") else None
    try:
        call = touched(event, runtime, folder)
    except Blocked as e:
        return _pre("deny", str(e)), None
    if not call.vaults:
        return _pre(None, updated=call.updated_input), None

    # 1. Focus
    allowed = [folder["writes"], *(folder.get("reads") or [])] if folder else []
    outside = [v for v in call.vaults if v not in allowed]
    if outside:
        if not folder:
            reason = "No vaults are set up for this folder."
        else:
            reason = f"`{outside[0]}` is not used in this folder. {_here(folder)}"
            if call.search_root:
                reason += f" {call.search_root} holds other vaults too: search a narrower folder."
        return _pre("deny", reason), None

    # 2. Label: this call's reads count before its writes (e.g. `cp vaultA/x vaultB/`)
    label = lbl.from_json(state["label"])
    for v in call.reads:
        label = lbl.narrow(label, _audience(runtime, v))

    # 3, 4. Writes
    decision, reasons = None, []
    on_leak = runtime.get("on_leak", "ask")
    me = runtime.get("me", "")
    for v in call.writes:
        people = lbl.new_people(label, _audience(runtime, v), me)
        if people:
            reasons.append(_leak_reason(state, runtime, v, people, call.reads))
            decision = "deny" if on_leak == "block" or decision == "deny" else "ask"
        elif v in (folder.get("reads") or []):
            note = " Bash commands that mention a vault count as writes to it; use Read, Grep or Glob to only read." if call.bash else ""
            reasons.append(f"`{v}` is a read vault in this folder, so writing to it asks first.{note}")
            decision = decision or "ask"
    if decision == "deny":
        return _pre("deny", " ".join(reasons) + ' (on_leak = "block")'), None

    new_state = dict(state)
    new_state["label"] = lbl.to_json(label)
    new_state["read"] = list(dict.fromkeys(list(state.get("read", [])) + call.reads))
    return _pre(decision, " ".join(reasons), call.updated_input), new_state


def decide(event: dict, runtime: dict, state: dict | None, project_dir: str = "", briefing: list[str] = ()):
    """Returns (hook output or None, the session's new state or None if unchanged)."""
    name = event.get("hook_event_name")
    project_dir = project_dir or event.get("cwd") or ""
    if name == "SessionStart":
        return _session_start(event, runtime, state, project_dir, list(briefing))
    if name == "PreToolUse":
        return _pre_tool(event, runtime, state, project_dir)
    if name == "UserPromptSubmit":
        return None, _prompt(event, runtime, state, project_dir)
    return None, None


# ---------------------------------------------------------------- I/O

def runtime_path() -> Path:
    return state_dir() / "runtime.json"


def _may_touch_vault(event: dict, runtime: dict | None) -> bool:
    """For errors: could this call touch a vault? When unsure, yes."""
    tool = event.get("tool_name") or ""
    if tool.startswith(plugins.TOOL_PREFIXES):
        return True
    if runtime is None:
        return False  # vl isn't set up here; nothing to guard
    text = json.dumps(event.get("tool_input") or {}) + " " + (event.get("cwd") or "")
    homes = {_home(), os.path.realpath(_home())}
    for name, v in runtime.get("vaults", {}).items():
        for p in v.get("paths", []):
            tails = [p] + [p[len(h) + 1:] for h in homes if inside(p, h) and p != h]
            if name in text or any(t in text for t in tails):
                return True
    return tool in SEARCH_TOOLS


def run(event: dict, env: dict | None = None) -> dict | None:
    env = os.environ if env is None else env
    name = event.get("hook_event_name")
    if name not in ("SessionStart", "PreToolUse", "UserPromptSubmit"):
        return None
    path = runtime_path()
    if not path.exists():  # vl isn't set up: only Basic Memory calls might reach a vault
        if name == "PreToolUse" and _may_touch_vault(event, None):
            return _pre("deny", f"{ERROR} (runtime.json is missing)", prefix="")
        return None
    try:
        runtime = json.loads(path.read_text())
        runtime["vaults"], runtime["folders"]
    except Exception as e:  # noqa: BLE001 - can't tell where the vaults are, so every matched call might touch one
        if name == "PreToolUse":
            return _pre("deny", f"{ERROR} (runtime.json can't be read: {e})", prefix="")
        return None
    if runtime.get("version") != RUNTIME_VERSION:  # its layout may hide calls vl should check
        if name == "PreToolUse" and _may_touch_vault(event, runtime):
            return _pre("deny", "runtime.json is from another version of vl. Run `vl apply`.")
        return None
    try:
        project_dir = env.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ""
        session_id = str(event.get("session_id") or "unknown")
        briefing = [v for _, plugin, data in plugins.enabled(runtime) if hasattr(plugin, "context_vaults")
                    for v in plugin.context_vaults(project_dir, data)] if name == "SessionStart" else []
        with lbl.session(state_dir(), session_id) as box:
            out, new_state = decide(event, runtime, box[0], project_dir, briefing)
            if new_state is not None:
                box[0] = new_state
        return out
    except Exception as e:  # noqa: BLE001 - fail closed for anything that may touch a vault
        if name == "PreToolUse" and _may_touch_vault(event, runtime):
            return _pre("deny", f"{ERROR} ({type(e).__name__}: {e})", prefix="")
        return None


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    out = run(event)
    if out:
        sys.stdout.write(json.dumps(out))
    sys.exit(0)
