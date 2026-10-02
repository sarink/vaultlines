"""The `vl` command."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    __version__,
    audience,
    claude,
    config,
    github,
    gitsync,
    launchd,
    obsidian,
    plugins,
    runtime,
)
from . import label as lbl
from . import vaults as vlt
from .audience import Audience
from .config import Config
from .util import (
    VlError,
    cache_dir,
    clones_path,
    contract,
    notify,
    read_json,
    say,
    state_dir,
    vaults_dir,
    vl_home,
    warn,
)


def _table(rows: list[tuple[str, ...]], indent: str = "  ") -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        say(indent + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


# ---------------------------------------------------------------- who you are

def _me() -> str:
    """Your GitHub login, from the last check or from GitHub."""
    me = audience.cached_me()
    if not me:
        me = github.login()
        if me:
            state = audience.load_state()
            state["me"] = me
            audience.save_state(state)
    if not me:
        raise VlError("vl doesn't know your GitHub login yet. Run `vl init`.")
    return me


def _initialized() -> bool:
    me = audience.cached_me()
    return bool(me) and (vaults_dir() / me).is_dir() and config.config_path().exists()


def _owner(text: str) -> str:
    owner = text.strip().lower()
    if not re.fullmatch(vlt.OWNER_RE, owner):
        raise VlError(f"{text!r} isn't a GitHub user or organization name.")
    return owner


# ---------------------------------------------------------------- vaults on disk

def _new_vault(vault_id: str, about: str) -> Path:
    """A new vault on this computer: a git repo with vault.toml."""
    path = vlt.path_of(vault_id)
    if path.exists() and any(path.iterdir()):
        raise VlError(f"{contract(path)} already exists.")
    path.mkdir(parents=True, exist_ok=True)
    (path / vlt.VAULT_FILE).write_text(vlt.render_vault_toml(about))
    gitsync.init_repo(path)
    return path


def _clone(vault_id: str, url: str) -> None:
    path = vlt.path_of(vault_id)
    say(f"Cloning {vault_id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    gitsync.clone(url, path)
    if not gitsync.has_commits(path):
        gitsync.init_repo(path)  # an empty repo: this is the first computer to use it
    gitsync.ensure_identity(path)
    gitsync.ensure_local_rules(path)


def _discovery() -> dict:
    return audience.load_state().get("discovery", {})


def _record(owner: str, found) -> None:
    state = audience.load_state()
    state.setdefault("discovery", {})[owner] = {"at": time.time(), "vaults": sorted(found)}
    audience.save_state(state)


def _forget(owner: str) -> None:
    state = audience.load_state()
    state.get("discovery", {}).pop(owner, None)
    audience.save_state(state)


def lost(cfg: Config) -> set[str]:
    """Vaults on GitHub that the last check didn't find: you can't access them any more."""
    found = _discovery()
    return {vid for vid, v in cfg.vaults.items()
            if not v.local and v.remote and v.owner in found and vid not in found[v.owner]["vaults"]}


def _personal_about(owner: str, me: str) -> str:
    return f"{me}'s personal notes." if owner == me else f"{me}'s personal notes in {owner}."


def _join(owner: str, me: str) -> list[str]:
    """Clone the owner's vaults that aren't here yet, and make your personal vault for the
    owner if there's none. Returns the new vaults."""
    try:
        found = github.vault_repos(owner)
    except github.Unreachable as e:
        raise VlError(f"Couldn't ask GitHub for {owner}'s vaults: {e}") from None
    (vaults_dir() / owner).mkdir(parents=True, exist_ok=True)
    _record(owner, found)
    new = []
    for vid, url in found.items():
        if not vlt.path_of(vid).exists():
            _clone(vid, url)
            new.append(vid)
    personal = vlt.personal_id(owner, me)
    if not vlt.path_of(personal).exists():
        _new_vault(personal, _personal_about(owner, me))
        new.append(personal)
        say(f"Made your personal vault {personal}, on this computer only. "
            f"To put it on GitHub: `vl vault publish {personal}`")
    return new


def _publish(cfg: Config, vault_id: str) -> None:
    v = cfg.vault(vault_id)
    if v.id != vlt.personal_id(v.owner, cfg.me or _me()):
        raise VlError(f"`vl vault publish` is for only your personal vault, like "
                      f"{vlt.personal_id(v.owner, cfg.me or _me())}. {v.id} isn't one.")
    if v.remote:
        raise VlError(f"{v.id} is already on GitHub ({v.remote}).")
    say(f"Creating the private GitHub repo {v.id}")
    github.create_private_repo(v.id, v.path)
    found = _discovery().get(v.owner, {}).get("vaults", [])
    _record(v.owner, [*found, v.id])
    a = github.audience(v.id)
    others = [x for x in a.logins if x.lower() != (cfg.me or _me())]
    if a.kind == "people" and others:
        warn(f"GitHub says these people can also see {v.id}: {', '.join(others)} "
             "(organization owners, or the organization's base permission).")


# ---------------------------------------------------------------- init / org

REQUIRED = {
    "git": "https://git-scm.com (on macOS: xcode-select --install)",
    "claude": "https://claude.com/claude-code",
    "gh": "https://cli.github.com (on macOS: brew install gh)",
}


def cmd_init(args) -> None:
    basic_memory = not args.no_basic_memory and config.load_file().basic_memory
    required = dict(REQUIRED)
    if os.environ.get("VAULTLINES_FAKE_GITHUB"):
        required.pop("gh")
    if basic_memory:
        required["uvx"] = "https://docs.astral.sh/uv/ (on macOS: brew install uv)"
    missing = [f"  {tool}: {hint}" for tool, hint in required.items() if not shutil.which(tool)]
    if missing:
        raise VlError("Install these first:\n" + "\n".join(missing))
    if not github.logged_in():
        say("Sign in to GitHub. vl uses your account to find the vaults you can access.")
        subprocess.run(["gh", "auth", "login"])
        if not github.logged_in():
            raise VlError("Not signed in to GitHub. Run `gh auth login`, then `vl init` again.")
    me = github.login()
    if not me:
        raise VlError("Couldn't get your GitHub login. Check `gh auth status`.")
    state = audience.load_state()
    state["me"] = me
    audience.save_state(state)
    if config.write_template(basic_memory=basic_memory):
        say(f"Wrote {contract(config.config_path())}: comments and examples only, for your own changes.")

    cfg = config.load()
    for _, plugin, settings in plugins.configured(cfg):
        if hasattr(plugin, "init"):
            plugin.init(cfg, settings)
    _join(me, me)
    if args.publish:
        _publish(config.load(), vlt.personal_id(me, me))
    _apply(config.load())
    say("Claude Code hooks installed: every session is guarded by vl.")
    cfg = config.load()
    if launchd.supported():
        launchd.install(cfg.sync_interval)
        say(f"Sync runs every {cfg.sync_interval // 60} min. Log: {contract(launchd.log_path())}")
    elif sys.platform != "darwin":
        say(f"No background sync on this system yet. Add a cron job: */10 * * * * {shutil.which('vl') or 'vl'} sync --background")
    say("\nDone. Next: `vl org join OWNER` for an organization's vaults. See `vl status`.")


def cmd_org_join(args) -> None:
    owner = _owner(args.owner)
    if not _initialized():
        say("Setting up vl on this computer first.")
        cmd_init(argparse.Namespace(publish=False, no_basic_memory=False))
    me = _me()
    if owner != me and github.owner_kind(owner) is None:
        raise VlError(f"GitHub has no user or organization named {owner} that you can see.")
    fresh = not (vaults_dir() / owner).exists()
    try:
        _join(owner, me)
    except VlError:
        if fresh:
            shutil.rmtree(vaults_dir() / owner, ignore_errors=True)
        raise
    cfg = config.load()
    for module in plugins.SOURCES.values():
        if hasattr(module, "joined"):
            module.joined(cfg, owner)
    _apply(cfg)
    shorts = cfg.shorts
    say(f"\nJoined {owner}. Its vaults on this computer:")
    _table([(vid, shorts[vid], v.about or "-") for vid, v in sorted(cfg.vaults.items()) if v.owner == owner])
    say(f"\nRun `claude` in any {owner} repo: vl picks its vaults. See `vl status`.")


def _unique(path: Path) -> Path:
    n, found = 2, path
    while found.exists():
        found = path.with_name(f"{path.name}-{n}")
        n += 1
    return found


def cmd_org_leave(args) -> None:
    owner = _owner(args.owner)
    me = _me()
    if owner == me:
        raise VlError("That's your own account. vl always keeps it: your personal vault lives there.")
    folder = vaults_dir() / owner
    if not folder.is_dir():
        raise VlError(f"You haven't joined {owner}.")
    cfg = config.load()
    unpublished = sorted(vid for vid, v in cfg.vaults.items() if v.owner == owner and not v.remote)
    if args.delete_files:
        shutil.rmtree(folder)
        say(f"Deleted {contract(folder)}")
    else:
        dest = _unique(vl_home() / "left" / owner)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(dest))
        say(f"Stopped syncing {owner}. Its files are in {contract(dest)}")
        for vid in unpublished:
            say(f"  {vid} was never published: its notes are only in {contract(dest / vid.split('/')[1])}")
    _forget(owner)
    _apply(config.load())


# ---------------------------------------------------------------- vault create / publish

def cmd_vault_create(args) -> None:
    vault_id = args.vault.strip().lower()
    if not vlt.valid_id(vault_id):
        raise VlError("Give OWNER/vault-NAME, like mixim-ai/vault-design (or local/NAME for this computer only).")
    owner, repo = vault_id.split("/", 1)
    about = args.about or ""
    cfg = config.load()
    if vault_id in cfg.vaults or vlt.path_of(vault_id).exists():
        raise VlError(f"{vault_id} is already on this computer.")
    if owner == vlt.LOCAL:
        if not config.LOCAL_RE.match(vault_id):
            raise VlError("Local vault names use lowercase letters, digits and dashes, like local/recipes.")
        _new_vault(vault_id, about)
        config.add_local_vault(vault_id)
        say(f"Made {vault_id} at {contract(vlt.path_of(vault_id))}, on this computer only. "
            f"It's listed in {contract(config.config_path())}.")
        _apply(config.load())
        return
    if not repo.startswith(vlt.PREFIX):
        raise VlError(f"Vault repos start with {vlt.PREFIX}, like {owner}/{vlt.PREFIX}{repo}.")
    if owner not in cfg.owners:
        raise VlError(f"You haven't joined {owner}. Run `vl org join {owner}` first.")
    if github.exists(vault_id):
        raise VlError(f"{vault_id} already exists on GitHub.")
    path = _new_vault(vault_id, about)
    say(f"Creating the private GitHub repo {vault_id}")
    try:
        github.create_private_repo(vault_id, path)
    except VlError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    found = _discovery().get(owner, {}).get("vaults", [])
    _record(owner, [*found, vault_id])
    _apply(config.load())
    say(f"Made {vault_id}. Give people access to the repo on GitHub; `vl sync` finds it for them.")
    say(f"To send a repo's notes there, add it to notes_from in {vault_id}'s vault.toml.")


def cmd_vault_publish(args) -> None:
    cfg = config.load()
    _publish(cfg, args.vault)
    _apply(config.load())
    say(f"Published {cfg.vault(args.vault).id}: a private repo that only you can access.")


# ---------------------------------------------------------------- apply

@dataclass
class Applied:
    errors: list[str] = field(default_factory=list)  # vaults GitHub couldn't be asked about


def clones() -> dict[str, str]:
    """Repos Claude ran in (recorded by the hook): top folder -> OWNER/REPO."""
    found = read_json(clones_path(), {})
    return {root: repo for root, repo in found.items() if isinstance(repo, str)}


def write_runtime(cfg: Config, fresh: bool, warnings: list[str] | None = None) -> tuple[dict[str, Audience], list[str]]:
    auds, errors = audience.audiences(cfg.vaults, fresh=fresh)
    cfg.me = audience.cached_me() or cfg.me
    data = {name: plugin.data(cfg, settings) if hasattr(plugin, "data") else {}
            for name, plugin, settings in plugins.configured(cfg)}
    runtime.write(runtime.build(cfg, auds, data, lost(cfg), warnings))
    return auds, errors


def _local_vaults(cfg: Config) -> None:
    """Make the local vaults listed in config.toml that aren't on disk yet."""
    for vid in cfg.local:
        if not vlt.path_of(vid).exists():
            _new_vault(vid, "")
            say(f"Made {vid} at {contract(vlt.path_of(vid))}")


def _apply(cfg: Config, fresh: bool = True) -> Applied:
    """Make git, the plugins, Claude Code and Obsidian match the vaults and config.toml."""
    warnings: list[str] = []
    _local_vaults(cfg)
    cfg = config.load()
    for vid, v in sorted(cfg.vaults.items()):
        warnings += [f"{vid}: vault.toml: {p}" for p in v.problems]
        gitsync.ensure_local_rules(v.path)
    if not cfg.me:
        cfg.me = github.login()

    saved = audience.load_state().get("plugins", {})
    plugin_state = {}
    for name, plugin, settings in plugins.configured(cfg):
        before = saved.get(name, {})
        new = plugin.apply(cfg, settings, before.get("state", {}), warnings) if hasattr(plugin, "apply") else None
        plugin_state[name] = {"kind": settings["kind"], "state": new or {}}
    for name, before in saved.items():
        if name not in plugin_state:
            plugin = plugins.KINDS.get(before.get("kind"))
            if plugin and hasattr(plugin, "off"):
                plugin.off(before.get("state", {}), warnings)
    claude.install_hooks()

    _, errors = write_runtime(cfg, fresh, warnings)
    _refresh_clones(warnings)
    state = audience.load_state()
    state["plugins"] = plugin_state
    if fresh and not errors:
        state["checked_at"] = time.time()
    audience.save_state(state)
    warnings += hook_warnings(cfg)

    todo = obsidian.register([v.path for v in cfg.vaults.values() if v.path.exists()])
    if todo:
        say("Obsidian is open, so these vaults weren't added to it. Close Obsidian and run `vl apply`:")
        for p in todo:
            say(f"  {contract(p)}")
    for w in dict.fromkeys(warnings):
        warn(w)
    for e in errors:
        warn(e)
    return Applied(errors)


def _refresh_clones(warnings: list[str]) -> None:
    """Set each repo Claude ran in up again for its rules (like Basic Memory's block there),
    so a change, like a new notes_from, shows before the next session starts."""
    from . import rules

    data = runtime.load() or {}
    for root in sorted({*clones(), *config.load_file().folders}):
        if not Path(root).is_dir():
            continue
        found = rules.resolve(root, data)
        for name, plugin, pdata in plugins.enabled(data):
            if hasattr(plugin, "session_start"):
                try:
                    plugin.session_start(found, data, pdata)
                except (OSError, VlError) as e:
                    warnings.append(f"{contract(root)}: couldn't set up {name}: {e}")


def cmd_apply(args) -> None:
    result = _apply(config.load())
    say("Applied." if not result.errors else "Applied, with the warnings above.")


def hook_warnings(cfg: Config) -> list[str]:
    """Places Claude runs in where a settings file turns every hook off."""
    out = []
    seen = set()
    for folder in sorted({*clones(), *cfg.folders}):
        if not Path(folder).is_dir():
            continue
        for path in claude.disables_hooks(folder):
            if path not in seen:
                seen.add(path)
                out.append(f"{contract(path)} sets disableAllHooks, so vl can't guard vaults in sessions there")
    return out


def cmd_uninstall(args) -> None:
    claude.remove_hooks()
    launchd.uninstall()
    say("Removed vl's Claude Code hooks and stopped background sync. Vaults, settings and notes are untouched.")


# ---------------------------------------------------------------- sync

def _vault_tomls(cfg: Config) -> dict[str, str]:
    out = {}
    for vid, v in cfg.vaults.items():
        try:
            out[vid] = (v.path / vlt.VAULT_FILE).read_text()
        except OSError:
            out[vid] = ""
    return out


def _check_github(cfg: Config, stamp) -> list[str]:
    """The daily check, first part: find new vaults and lost ones. Returns the new vaults."""
    new = []
    for owner in cfg.owners:
        try:
            new += _join(owner, cfg.me or _me())
        except VlError as e:
            say(f"{stamp()}check: {owner}: {str(e).splitlines()[-1]}")
    for vid in new:
        say(f"{stamp()}{vid}: new vault")
    return new


def clean_fetched(max_age: float = 86400) -> int:
    """Delete originals fetched more than `max_age` seconds ago. Returns how many."""
    root = cache_dir() / "fetch"
    removed = 0
    cutoff = time.time() - max_age
    for dirpath, _, files in os.walk(root, topdown=False):
        for name in files:
            path = Path(dirpath) / name
            try:
                if path.lstat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        if Path(dirpath) != root:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass  # not empty
    return removed


def _sync_vault(v: vlt.Vault, stamp) -> str:
    if v.source is not None:
        source = plugins.SOURCES.get(v.source.get("kind"))
        if source and hasattr(source, "sync"):
            return source.sync(v, stamp)
        return gitsync.pull_keeping_changes(v.path)
    return gitsync.sync(v.path)


def cmd_sync(args) -> None:
    cfg = config.load()
    only = cfg.vault(args.vault) if args.vault else None
    lock_file = state_dir() / "sync.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("Another sync is running.")
            return
        stamp = (lambda: time.strftime("%Y-%m-%d %H:%M:%S ")) if args.background else (lambda: "")
        daily = not only and time.time() - audience.load_state().get("checked_at", 0) >= cfg.check_interval
        before = _vault_tomls(cfg)
        if daily:
            _check_github(cfg, stamp)
            cfg = config.load()
        lbl.clean_sessions(state_dir())
        clean_fetched()
        gone = lost(cfg)
        failed = []
        for vid, v in sorted(cfg.vaults.items()):
            if only and vid != only.id:
                continue
            if vid in gone:
                say(f"{stamp()}{vid}: no access on GitHub any more, so it isn't synced. Its files stay.")
                continue
            try:
                status = _sync_vault(v, stamp)
                if not args.background or status != "synced":
                    say(f"{stamp()}{vid}: {status}")
            except VlError as e:
                failed.append(vid)
                say(f"{stamp()}{vid}: FAILED: {e}")
        if not only:
            _auto_pull(cfg, stamp, args.background)
        cfg = config.load()
        if daily:
            _apply(cfg, fresh=True)
            _report_hook_warnings(cfg, stamp, args.background)
        elif _vault_tomls(cfg) != before:
            _apply(cfg, fresh=False)  # a vault.toml changed: notes_from, about or source
        if args.background and failed:
            notify(f"Couldn't sync: {', '.join(failed)}. Run `vl sync` to see why.")
        if failed:
            sys.exit(1)


def _auto_pull(cfg: Config, stamp, background: bool) -> None:
    wanted = {key for key, rule in cfg.repos.items() if rule.auto_pull}
    for root, repo in sorted(clones().items()):
        if repo not in wanted or not Path(root).is_dir():
            continue
        try:
            gitsync.pull_only(Path(root))
            if not background:
                say(f"{contract(root)}: pulled")
        except VlError as e:
            say(f"{stamp()}{contract(root)}: pull skipped: {str(e).splitlines()[-1]}")


def _report_hook_warnings(cfg: Config, stamp, background: bool) -> None:
    state = audience.load_state()
    before = set(state.get("hook_warnings", []))
    now = hook_warnings(cfg)
    state["hook_warnings"] = now
    audience.save_state(state)
    new = [w for w in now if w not in before]
    for w in new:
        say(f"{stamp()}check: {w}")
    if new and background:
        notify(f"vl can't guard some places: {len(new)} settings file(s) set disableAllHooks. Run `vl check`.")


# ---------------------------------------------------------------- check / status / sessions / doctor

def preview(writes: str, reads: list[str], auds: dict[str, Audience], me: str) -> list[str]:
    """Where writes will ask, given who can see each vault. Vaults are short names."""
    def aud(name: str) -> dict:
        a = auds.get(name) or audience.unknown("not checked yet")
        return {"kind": a.kind, "logins": list(a.logins), "reason": a.reason}

    lines = []
    for r in reads:
        people = lbl.new_people(lbl.narrow(lbl.EVERYONE, aud(r)), aud(writes), me)
        if people:
            lines.append(f"writes to {writes} ask after reading {r} ({lbl.names(people)} can't see {r})")
    return lines


def _owner_title(owner: str, me: str) -> str:
    if owner == me:
        return f"{owner} (your account)"
    return owner


def cmd_check(args) -> None:
    cfg = config.load()
    auds_by_id, errors = write_runtime(cfg, fresh=True)
    state = audience.load_state()
    if not errors:
        state["checked_at"] = time.time()
    state["hook_warnings"] = hook_warnings(cfg)
    audience.save_state(state)
    me = audience.cached_me()
    data = runtime.load() or {}
    shorts = cfg.shorts
    auds = {shorts[vid]: a for vid, a in auds_by_id.items()}
    say("Who can see each vault")
    _table([("vault", "who can see it")] + [(vid, a.describe()) for vid, a in sorted(auds_by_id.items())])
    say("\nWhere writes will ask")
    any_line = False
    for owner, o in sorted((data.get("owners") or {}).items()):
        targets = sorted({o["personal"], *o["notes_from"].values()} - {None})
        for w in targets:
            for line in preview(w, [v for v in o["vaults"] if v != w], auds, me):
                say(f"  {line}")
                any_line = True
    if not any_line:
        say("  never")
    for w in state["hook_warnings"]:
        warn(w)
    for e in errors:
        warn(e)


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def _vault_state(v: vlt.Vault, gone: set[str]) -> str:
    if v.id in gone:
        return "no access on GitHub any more (not synced)"
    if not (v.path.exists() and gitsync.is_repo(v.path)):
        return "MISSING"
    pending = gitsync.pending_changes(v.path)
    where = "on GitHub" if v.remote else "this computer only"
    text = f"{where}, last commit {gitsync.last_commit_age(v.path)}" + (f", {pending} unsaved" if pending else "")
    source = plugins.SOURCES.get((v.source or {}).get("kind"))
    if source and hasattr(source, "status"):
        text = source.status(v)
    return text


def cmd_status(args) -> None:
    cfg = config.load()
    if not cfg.vaults:
        say("No vaults yet. Run `vl init`.")
        return
    me = cfg.me
    auds, _ = audience.audiences(cfg.vaults, fresh=False)
    shorts = cfg.shorts
    gone = lost(cfg)
    data = runtime.load() or {}
    owners = sorted({v.owner for v in cfg.vaults.values()} | set(cfg.owners), key=lambda o: (o != me, o == "local", o))
    for owner in owners:
        say(_owner_title(owner, me))
        rows = [("vault", "name", "state", "who can see it")]
        for vid, v in sorted(cfg.vaults.items()):
            if v.owner == owner:
                rows.append((vid, shorts[vid], _vault_state(v, gone), auds[vid].describe()))
        if len(rows) > 1:
            _table(rows)
        o = (data.get("owners") or {}).get(owner)
        if o:
            for repo, short in o["notes_from"].items():
                say(f"  notes from {repo} -> {short}")
            for repo, claims in o["conflicts"].items():
                say(f"  CONFLICT: {repo} is in notes_from of {' and '.join(claims)}, so its notes go to "
                    f"{o['personal']}. Remove it from all but one vault.toml.")
            if o["personal"]:
                say(f"  other {owner} repos -> {o['personal']}")
        say("")
    if cfg.repos or cfg.folders:
        say(f"Your changes ({contract(config.config_path())})")
        rows = [("repo or folder", "writes", "reads", "auto_pull")]
        for rule in cfg.rules():
            rows.append((contract(rule.key), rule.writes or "-", ", ".join(rule.reads) or "-",
                         "yes" if rule.auto_pull else ""))
        _table(rows)
        say("")
    default = (data.get("default") or {}).get("writes")
    say(f"Anywhere else -> {default or 'nothing (run `vl init`)'}")
    hooks = claude.hooks_installed()
    say(f"Claude Code hooks: {'installed' if len(hooks) == len(claude.HOOK_EVENTS) else 'MISSING (run `vl apply`)'}")
    problem = runtime.stale(data or None)
    if problem:
        say(f"Hook data: {problem} (run `vl apply`)")
    for w in hook_warnings(cfg):
        warn(w)
    checked = audience.load_state().get("checked_at")
    say(f"Last checked with GitHub: {time.strftime('%Y-%m-%d %H:%M', time.localtime(checked)) if checked else 'never'}")
    if launchd.supported():
        say(f"Background sync: {'on' if launchd.loaded() else 'OFF (run `vl init`)'}, every {cfg.sync_interval // 60} min")


def cmd_sessions(args) -> None:
    sessions = lbl.all_sessions(state_dir())[: args.limit]
    if not sessions:
        say("No sessions recorded yet.")
        return
    rows = [("updated", "session", "repo or folder", "writes", "label", "read")]
    for sid, s in sessions:
        rules = s.get("rules") or {}
        rows.append((
            time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("updated", 0))),
            sid[:8],
            rules.get("repo") or contract(rules.get("folder") or "-"),
            rules.get("writes") or "-",
            lbl.describe(lbl.from_json(s.get("label")), audience.cached_me()),
            ", ".join(s.get("read", [])) or "-",
        ))
    _table(rows)


def cmd_doctor(args) -> None:
    cfg = config.load()
    ok = True

    def check(passed: bool, text: str, fix: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        say(f"  {'ok  ' if passed else 'FAIL'}  {text}" + ("" if passed or not fix else f"  -> {fix}"))

    say("Tools")
    for tool in ("git", "claude", "gh"):
        found = shutil.which(tool)
        check(bool(found), f"{tool}: {found or 'not found'}")
    check(bool(cfg.me), f"GitHub login: {cfg.me or 'unknown'}", "vl init")
    say("Vaults")
    check(bool(cfg.me) and cfg.personal(cfg.me) is not None, "your personal vault is here", "vl init")
    for vid, v in sorted(cfg.vaults.items()):
        check(gitsync.is_repo(v.path), f"{vid} is a git repo", "vl apply")
        if v.remote:
            check(vlt.remote_id(v.remote) == vid, f"{vid} syncs to its GitHub repo",
                  f"its git remote is {v.remote}; fix it with `git remote set-url origin`")
        for p in v.problems:
            check(False, f"{vid}: vault.toml: {p}", "ask whoever manages the vault")
    for _, plugin, settings in plugins.configured(cfg):
        if hasattr(plugin, "doctor"):
            plugin.doctor(cfg, settings, check)
    for module in plugins.SOURCES.values():
        if hasattr(module, "doctor_sources"):
            module.doctor_sources(cfg, check)
    say("Claude Code")
    hooks = claude.hooks_installed()
    for event in claude.HOOK_EVENTS:
        check(event in hooks, f"{event} hook runs `vl hook`", "vl apply")
    for w in hook_warnings(cfg):
        check(False, w, "remove disableAllHooks there, or accept that vl can't guard those sessions")
    data = runtime.load()
    problem = runtime.stale(data)
    check(problem is None, "hook data (runtime.json) is up to date", "vl apply" if problem else "")
    if problem is None:
        unchecked = [n for n, v in data["vaults"].items() if v["audience"].get("reason") == "not checked yet"]
        check(not unchecked, "every vault's audience is known to the hook", "vl check")
    if launchd.supported():
        say("Sync")
        check(launchd.loaded(), "background sync is on", "vl init")
    say("\nAll good." if ok else "\nSome checks failed.")
    if not ok:
        sys.exit(1)


def cmd_migrate(args) -> None:
    from .migrate import migrate

    migrate(args)


def cmd_hook(args) -> None:
    from .hook import main as hook_main

    hook_main()


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vl", description="Notes for Claude Code, shared through GitHub.")
    p.add_argument("--version", action="version", version=f"vaultlines {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="set up this computer: GitHub sign-in, your personal vault, hooks")
    s.add_argument("--publish", action="store_true", help="also put your personal vault on GitHub (private)")
    s.add_argument("--no-basic-memory", action="store_true", help="don't set up Basic Memory")
    s.set_defaults(func=cmd_init)

    org = sub.add_parser("org", help="join or leave a GitHub organization's vaults")
    osub = org.add_subparsers(dest="org_command", required=True, metavar="ACTION")
    s = osub.add_parser("join", help="get the vaults of a GitHub organization (or user) you can access")
    s.add_argument("owner")
    s.set_defaults(func=cmd_org_join)
    s = osub.add_parser("leave", help="stop syncing an owner's vaults (keeps files unless --delete-files)")
    s.add_argument("owner")
    s.add_argument("--delete-files", action="store_true")
    s.set_defaults(func=cmd_org_leave)

    vault = sub.add_parser("vault", help="create or publish a vault")
    vsub = vault.add_subparsers(dest="vault_command", required=True, metavar="ACTION")
    s = vsub.add_parser("create", help="a new private vault repo with vault.toml (or local/NAME)")
    s.add_argument("vault", metavar="OWNER/vault-NAME")
    s.add_argument("--about", help="one line about the vault, for Claude")
    s.set_defaults(func=cmd_vault_create)
    s = vsub.add_parser("publish", help="put your local personal vault on GitHub, private")
    s.add_argument("vault", metavar="OWNER/vault-ME-personal")
    s.set_defaults(func=cmd_vault_publish)
    s = vsub.add_parser("list", help="same as `vl status`")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("sync", help="sync vaults now (and once a day, ask GitHub for new vaults)")
    s.add_argument("vault", nargs="?", help="only this vault")
    s.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("sessions", help="recent Claude sessions and what they read (for debugging)")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    sub.add_parser("check", help="ask GitHub who can see each vault, and show where writes will ask").set_defaults(func=cmd_check)
    sub.add_parser("apply", help="set up git, Basic Memory and Claude Code again").set_defaults(func=cmd_apply)
    sub.add_parser("status", help="show vaults by owner, your changes, hooks and sync").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="check that everything is set up").set_defaults(func=cmd_doctor)
    sub.add_parser("uninstall", help="remove the hooks and stop background sync").set_defaults(func=cmd_uninstall)
    s = sub.add_parser("migrate", help="move a vl 0.3 setup into ~/.vaultlines (once)")
    s.add_argument("--map", action="append", metavar="OLD=OWNER/REPO",
                   help="where an old vault on this computer only goes, like mixim-private=mixim-ai/vault-private")
    s.add_argument("--dry-run", action="store_true", help="show what would happen, and change nothing")
    s.set_defaults(func=cmd_migrate)
    sub.add_parser("hook", help=argparse.SUPPRESS).set_defaults(func=cmd_hook)
    plugins.add_commands(sub)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except VlError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
