"""`vl hook`: the Claude Code hook that guards vaults.

Claude Code runs it on SessionStart and before matching tool calls (PreToolUse).
It reads runtime.json (written by `vl apply` and the daily check), never the
TOML config and never GitHub, so it stays fast.

At SessionStart it works out the session's rules (see rules.py): the vault it writes
to and the vaults it may read, from the repo where Claude started. The session record
keeps them, so a resumed session keeps them too. Then, for every call:

1. Focus: a call touching a vault that isn't the session's `writes` or `reads` is blocked.
2. Label: every vault read narrows the session label to the people who can see it.
3. Writes: a write to vault V asks (or blocks, with on_leak = "block") if people who
   can see V couldn't see everything the session read.
4. Writes to a `reads` vault always ask.
5. A vault filled from elsewhere (a [source], like Google Drive) is read-only.
6. Reads are recorded here, before the call runs.
7. A filled vault's fetch folder (originals from `vl gdrive fetch`) counts as the vault,
   for reads only. `vl gdrive fetch OWNER/REPO` is a read of that vault.
8. vl itself: writes to its records are blocked, and so is any access to its Google
   sign-ins; changes to config.toml, its hooks, or `vl` commands that change what it
   allows ask, unless your latest message mentions vl (UserPromptSubmit records that).

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
from .plugins.api import mentions
from .rules import resolve
from .util import clones_path, closest_parent, google_dir, inside, state_dir, vl_home

READ_TOOLS = {"Read": "file_path"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SEARCH_TOOLS = ("Grep", "Glob")
MATCHER = "^(" + "|".join(["Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob", "Bash",
                            *(f"{p}.*" for p in plugins.TOOL_PREFIXES)]) + ")$"
ERROR = "vl hook error: run `vl doctor`."
RUNTIME_VERSION = 4  # runtime.json's layout
FETCH_RE = re.compile(r"(?:^|[\s;&|(`])(?:\S*/)?vl\s+gdrive\s+fetch\s+['\"]?([A-Za-z0-9][A-Za-z0-9._/-]*)")
FETCHED = "fetched originals are read-only copies. Run `vl gdrive fetch` again for a fresh one."
REPO_HOWS = ("repos", "notes_from", "personal", "conflict")  # rules that came from the repo


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


def _fetch_paths(runtime: dict) -> dict[str, str]:
    """Fetch folder -> the vault whose originals it holds."""
    return {p: name for name, v in runtime["vaults"].items() for p in v.get("fetch", [])}


def vault_of(path: str, runtime: dict, folders: dict[str, str] | None = None) -> str | None:
    folders = _vault_paths(runtime) if folders is None else folders
    key = closest_parent(folders, path)
    return folders[key] if key else None


def _by_ref(runtime: dict, ref: str) -> str | None:
    """A vault's short name, from its ID (OWNER/REPO) or short name."""
    ref = ref.lower()
    if ref in runtime["vaults"]:
        return ref
    return next((name for name, v in runtime["vaults"].items() if v.get("id") == ref), None)


def _filled(runtime: dict, vault: str) -> bool:
    """A vault filled from elsewhere (a [source] in its vault.toml): read-only."""
    return bool(runtime["vaults"].get(vault, {}).get("source"))


def fetched_of(path: str, runtime: dict) -> str | None:
    """The vault a file in a fetch folder came from."""
    return vault_of(path, runtime, _fetch_paths(runtime))


def vaults_within(root: str, runtime: dict) -> list[str]:
    """Vaults (and fetch folders) in `root`, or the one `root` is in."""
    folders = {**_fetch_paths(runtime), **_vault_paths(runtime)}
    found = [name for p, name in folders.items() if inside(p, root)]
    own = vault_of(root, runtime, folders)
    return list(dict.fromkeys(([own] if own else []) + sorted(found)))


def _glob_root(pattern: str) -> str | None:
    """The fixed part of an absolute Glob pattern, e.g. ~/notes/x/**/*.md -> ~/notes/x."""
    if not pattern.startswith(("/", "~")):
        return None
    fixed = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
    return fixed if fixed.endswith("/") or fixed == pattern else os.path.dirname(fixed) or "/"


def _home() -> str:
    return os.path.expanduser("~")


def bash_vaults(command: str, cwd: str, runtime: dict, folders: dict[str, str] | None = None) -> list[str]:
    """Vaults a shell command mentions: by absolute path, ~/..., $HOME/..., or relative to cwd.
    `folders` (default: the vaults' folders) maps folders to vaults.

    Best effort: a command can always build a path in ways no scan can see.
    """
    folders = _vault_paths(runtime) if folders is None else folders
    found = []
    here = vault_of(os.path.realpath(cwd), runtime, folders) if cwd else None
    if here:
        found.append(here)
    homes = {_home(), os.path.realpath(_home())}
    cwd_real = os.path.realpath(cwd) if cwd else None
    for p, name in folders.items():
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


def touched(event: dict, runtime: dict, rules: dict | None) -> Call:
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
            fetched = None if v else fetched_of(_abs(path, cwd), runtime)
            if fetched and kind == "write":
                raise Blocked(FETCHED)
            if v or fetched:
                call.add(v or fetched, kind)
    elif tool in SEARCH_TOOLS:
        # An absolute Glob pattern ignores the folder it runs in.
        pattern_root = _glob_root(args["pattern"]) if tool == "Glob" and isinstance(args.get("pattern"), str) else None
        roots = [pattern_root or args.get("path") or cwd]
        for root in roots:
            if not isinstance(root, str):
                continue
            found = vaults_within(_abs(root, cwd), runtime)
            if len(found) > 1 or (found and not (vault_of(_abs(root, cwd), runtime)
                                                 or fetched_of(_abs(root, cwd), runtime))):
                call.search_root = root
            for v in found:
                call.add(v, "read")
    elif tool == "Bash":
        command = args.get("command")
        if isinstance(command, str):
            for v in bash_vaults(command, cwd, runtime):
                call.add(v, "read")
                if not _filled(runtime, v):  # a filled vault is only read; sync undoes any change
                    call.add(v, "write")
                    call.bash = True
            # Originals from `vl gdrive fetch`, and `vl gdrive fetch` itself, only read.
            fetched = [_by_ref(runtime, ref.strip("'\"")) for ref in FETCH_RE.findall(command)]
            for v in bash_vaults(command, cwd, runtime, _fetch_paths(runtime)) + [v for v in fetched if v]:
                call.add(v, "read")
    elif found := plugins.plugin_for_tool(runtime, tool):
        plugin, data = found
        access = plugin.on_call(tool, args, rules, data)
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


def _audience(runtime: dict, vault: str) -> dict:
    return runtime["vaults"].get(vault, {}).get("audience") or {"kind": "unknown", "reason": "not in runtime.json"}


def _shorten(text: str, n: int = 60) -> str:
    if len(text) <= n:
        return text
    cut = text[: n + 1]
    cut = cut[: cut.rfind(" ")] if " " in cut else text[:n]
    return cut.rstrip(" ,.;:") + "…"


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def briefing_text(rules: dict, runtime: dict) -> str | None:
    """What Claude is told at SessionStart: where to save notes, what else it may read."""
    w = rules.get("writes")
    if not w:
        return None
    reads = rules.get("reads") or []
    vaults = runtime["vaults"]
    where = "this repo" if rules.get("how") in REPO_HOWS else "this folder"
    inline, extra = [], []
    for _, plugin, data in plugins.enabled(runtime):
        if hasattr(plugin, "briefing"):
            i, e = plugin.briefing(w, data)
            inline.append(i)
            extra.append(e)
    about = vaults.get(w, {}).get("about") or ""
    first = f"vaultlines: save notes from {where} to `{w}`" + (f" ({', '.join(inline)})" if inline else "")
    lines = [first + (f": {_sentence(about)}" if about else ".")]
    if rules.get("how") == "conflict":
        lines.append(f"{rules.get('repo')} is in notes_from of two vaults ({', '.join(rules.get('conflict') or [])}), "
                     f"so its notes go to your personal vault `{w}`. Ask whoever manages those vaults to keep it in one.")
    if reads:
        shown = []
        for r in reads:
            text = vaults.get(r, {}).get("about") or ""
            shown.append(f"`{r}`" + (f" ({_shorten(text)})" if text else ""))
        lines.append(f"You can also read: {', '.join(shown)}. Writing to those asks first.")
    lines.append(" ".join(["Other vaults are blocked here.", *extra]))
    for v in [w, *reads]:
        source = plugins.SOURCES.get(vaults.get(v, {}).get("source"))
        if source and hasattr(source, "source_briefing"):
            lines.append(source.source_briefing(v, vaults[v].get("id", v)))
    return " ".join(lines)


def _session_start(event: dict, runtime: dict, state: dict | None, project_dir: str, briefing: list[str],
                   rules: dict | None):
    source = event.get("source") or "startup"
    if rules is None:
        rules = resolve(project_dir, runtime)
    if state is None or source in ("startup", "clear"):
        if source in ("startup", "clear"):
            state = lbl.new_session(rules, lbl.EVERYONE, source)
        else:
            # A fork, or a resume/compact vl has no record of: the context may hold anything.
            state = lbl.new_session(rules, lbl.ONLY_YOU, source)
            state["why"] = "it was forked" if source == "fork" else f"vl has no record of it before this {source}"
    else:
        state = dict(state)  # a resumed session keeps its rules
    # Plugins (like Basic Memory's briefing) put notes from these vaults into the session.
    label = lbl.from_json(state["label"])
    read = list(state.get("read", []))
    for vault in briefing:
        label = lbl.narrow(label, _audience(runtime, vault))
        if vault not in read:
            read.append(vault)
    state.update(label=lbl.to_json(label), read=read)
    text = briefing_text(state.get("rules") or rules, runtime)
    return (_output("SessionStart", additionalContext=text) if text else None), state


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

VL_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|(`])(?:\S*/)?vl\s+(init|apply|uninstall|org|vault|gdrive\s+add)\b")
VL_ENV_RE = re.compile(r"\bVAULTLINES_[A-Z_]+")
GOOGLE = "Google sign-ins are for vl only. To get an original from Drive, run `vl gdrive fetch OWNER/REPO PATH`."


def _config_file(runtime: dict) -> str:
    return os.path.realpath(runtime.get("config") or str(vl_home() / "config.toml"))


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
    config = _config_file(runtime)
    google = os.path.realpath(str(google_dir()))
    path = args.get(READ_TOOLS.get(tool) or WRITE_TOOLS.get(tool) or "")
    if tool in SEARCH_TOOLS:
        path = (_glob_root(args["pattern"]) if tool == "Glob" and isinstance(args.get("pattern"), str) else None) \
            or args.get("path") or cwd
    if isinstance(path, str) and path:
        p = _abs(path, cwd)
        if inside(p, google) or (tool in SEARCH_TOOLS and inside(google, p)):
            return "deny", GOOGLE
    if tool == "Bash" and isinstance(args.get("command"), str) and mentions(args["command"], google):
        return "deny", GOOGLE
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
        if mentions(command, state) or mentions(command, config):
            return "ask", "This command touches vl's own files (its config or session records)."
        if VL_ENV_RE.search(command):
            return "ask", "This command sets a VAULTLINES_ variable, which changes where vl looks."
        m = VL_COMMAND_RE.search(command)
        if m:
            return "ask", f"This runs `vl {m.group(1)}`, which changes what vl allows. (Mention vl in your message to skip this question.)"
        if "disableAllHooks" in command:
            return "ask", "This command mentions disableAllHooks, which can switch vl off."
    return None


ASKED_VL_RE = re.compile(r"\b(vl|vaultlines)\b", re.IGNORECASE)


def _unknown_session(runtime: dict, project_dir: str) -> dict:
    state = lbl.new_session(resolve(project_dir, runtime), lbl.ONLY_YOU, "unknown")
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
    rules = state.get("rules") or resolve(project_dir, runtime)
    try:
        call = touched(event, runtime, rules)
    except Blocked as e:
        return _pre("deny", str(e)), None
    if not call.vaults:
        return _pre(None, updated=call.updated_input), None

    # 1. Focus
    allowed = [v for v in [rules.get("writes"), *(rules.get("reads") or [])] if v]
    outside = [v for v in call.vaults if v not in allowed]
    if outside:
        if not allowed:
            reason = "No vaults are set up here."
        else:
            reason = f"`{outside[0]}` isn't used here. {_here(rules)}"
            if call.search_root:
                reason += f" {call.search_root} holds other vaults too: search a narrower folder."
        return _pre("deny", reason), None

    # 5. Vaults filled from elsewhere are read-only
    for v in call.writes:
        if _filled(runtime, v):
            source = plugins.SOURCES.get(runtime["vaults"][v].get("source"))
            what = getattr(source, "NAME", None) or runtime["vaults"][v].get("source")
            return _pre("deny", f"`{v}` is filled from {what}, so it's read-only. Save notes in "
                                f"`{rules.get('writes')}`."), None

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
        elif v in (rules.get("reads") or []):
            note = " Bash commands that mention a vault count as writes to it; use Read, Grep or Glob to only read." if call.bash else ""
            reasons.append(f"`{v}` is a read vault here, so writing to it asks first.{note}")
            decision = decision or "ask"
    if decision == "deny":
        return _pre("deny", " ".join(reasons) + ' (on_leak = "block")'), None

    new_state = dict(state)
    new_state["label"] = lbl.to_json(label)
    new_state["read"] = list(dict.fromkeys(list(state.get("read", [])) + call.reads))
    return _pre(decision, " ".join(reasons), call.updated_input), new_state


def decide(event: dict, runtime: dict, state: dict | None, project_dir: str = "", briefing: list[str] = (),
           rules: dict | None = None):
    """Returns (hook output or None, the session's new state or None if unchanged).
    `rules`: the session's rules, if already worked out (SessionStart)."""
    name = event.get("hook_event_name")
    project_dir = project_dir or event.get("cwd") or ""
    if name == "SessionStart":
        return _session_start(event, runtime, state, project_dir, list(briefing), rules)
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
        runtime["vaults"], runtime["owners"]
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
        rules, briefing = (_start(project_dir, runtime) if name == "SessionStart" else (None, []))
        with lbl.session(state_dir(), session_id) as box:
            out, new_state = decide(event, runtime, box[0], project_dir, briefing, rules)
            if new_state is not None:
                box[0] = new_state
        return out
    except Exception as e:  # noqa: BLE001 - fail closed for anything that may touch a vault
        if name == "PreToolUse" and _may_touch_vault(event, runtime):
            return _pre("deny", f"{ERROR} ({type(e).__name__}: {e})", prefix="")
        return None


def _context_vaults(project_dir: str, runtime: dict) -> list[str]:
    return [v for _, plugin, data in plugins.enabled(runtime) if hasattr(plugin, "context_vaults")
            for v in plugin.context_vaults(project_dir, data)]


def _start(project_dir: str, runtime: dict) -> tuple[dict, list[str]]:
    """SessionStart's I/O: the rules, the plugins' setup for them, and what the plugins
    brief the session from. Plugins' own SessionStart hooks may run before or after this
    one, so both what they read before the setup and after it count as read."""
    rules = resolve(project_dir, runtime)
    before = _context_vaults(project_dir, runtime)
    for _, plugin, data in plugins.enabled(runtime):
        if hasattr(plugin, "session_start"):
            try:
                plugin.session_start(rules, runtime, data)
            except Exception:  # noqa: BLE001, S110 - setup is a convenience; the checks don't depend on it
                pass
    after = _context_vaults(project_dir, runtime)
    if rules.get("how") in REPO_HOWS and rules.get("root") and rules.get("repo"):
        _record_clone(rules["root"], rules["repo"])
    return rules, list(dict.fromkeys(before + after))


def _record_clone(root: str, repo: str) -> None:
    """Remember where a repo is cloned, so `vl apply` keeps its setup current and auto_pull finds it."""
    import fcntl

    path = clones_path()
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        found = {}
    if found.get(root) == repo:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            found = json.loads(path.read_text())
        except (OSError, ValueError):
            found = {}
        found[root] = repo
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
        tmp.replace(path)


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    out = run(event)
    if out:
        sys.stdout.write(json.dumps(out))
    sys.exit(0)
