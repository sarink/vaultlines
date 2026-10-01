"""The `vl` command."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, audience, claude, config, gitsync, launchd, obsidian, plugins, runtime
from . import label as lbl
from .audience import Audience
from .config import NAME_RE, Config, Folder, Vault, folder_for, show
from .util import (
    VlError,
    cache_dir,
    contract,
    expand,
    fetch_dir,
    notify,
    read_json,
    say,
    state_dir,
    warn,
)

OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def _table(rows: list[tuple[str, ...]], indent: str = "  ") -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        say(indent + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


# ---------------------------------------------------------------- vaults

def _check_new_name(cfg: Config, name: str) -> None:
    if not NAME_RE.match(name):
        raise VlError("Vault names use lowercase letters, digits and dashes (e.g. acme-everyone).")
    if name in cfg.vaults:
        raise VlError(f"A vault named '{name}' already exists.")


def _check_owner_repo(owner_repo: str) -> None:
    if not OWNER_REPO_RE.match(owner_repo):
        raise VlError(f"--github takes OWNER/REPO, like acme/acme-notes, not {owner_repo}")


def create_vault(cfg: Config, name: str, github: str | None, local: bool, path: str | None) -> Vault:
    _check_new_name(cfg, name)
    vault_path = expand(path) if path else cfg.vaults_dir / name
    if vault_path.exists() and any(vault_path.iterdir()) and not gitsync.is_repo(vault_path):
        raise VlError(f"{contract(vault_path)} already has files. Use `vl vault adopt` instead.")
    url = None
    if not local:
        if not github:
            login = audience.me()
            if not login:
                raise VlError("Not signed in to GitHub. Run `gh auth login`, pass --github OWNER/REPO, or use --local.")
            github = f"{login}/vault-{name}"
        _check_owner_repo(github)
    gitsync.init_repo(vault_path)
    if github:
        say(f"Creating private GitHub repo {github}")
        url = gitsync.create_github_repo(vault_path, github)
    cfg.vaults[name] = Vault(name, vault_path, url)
    say(f"Created vault '{name}' at {contract(vault_path)}" + (f", synced to {url}" if url else ", on this computer only"))
    return cfg.vaults[name]


def cmd_vault_create(args) -> None:
    cfg = config.load()
    create_vault(cfg, args.name, args.github, args.local, args.path)
    config.save(cfg)
    _apply(cfg)


def cmd_vault_join(args) -> None:
    cfg = config.load()
    gitsync.check_remote(args.url)
    name = args.name or re.sub(r"\.git$", "", args.url.rstrip("/").split("/")[-1])
    _check_new_name(cfg, name)
    path = expand(args.path) if args.path else cfg.vaults_dir / name
    say(f"Cloning {args.url}")
    gitsync.clone(args.url, path)
    if gitsync.git(path, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
        gitsync.init_repo(path)  # an empty repo: this is the first computer to use it
    gitsync.ensure_identity(path)
    cfg.vaults[name] = Vault(name, path, args.url)
    config.save(cfg)
    say(f"Joined vault '{name}' at {contract(path)}")
    _apply(cfg)


def cmd_vault_adopt(args) -> None:
    cfg = config.load()
    path = expand(args.path)
    if not path.is_dir():
        raise VlError(f"Folder not found: {args.path}")
    name = args.name or path.name
    _check_new_name(cfg, name)
    if args.github:
        _check_owner_repo(args.github)
    if not gitsync.is_repo(path):
        gitsync.init_repo(path)
    gitsync.ensure_identity(path)
    origin = gitsync.remote_url(path)
    url = None
    if args.github:
        if origin:
            raise VlError(f"{contract(path)} already syncs to {origin}. Drop --github.")
        say(f"Creating private GitHub repo {args.github}")
        url = gitsync.create_github_repo(path, args.github)
    elif origin:
        if gitsync.test_remotes() and origin.startswith("file://"):
            url = origin
        else:
            try:
                url = gitsync.github_url("/".join(gitsync.parse_github(origin)))
            except VlError:
                raise VlError(f"{contract(path)} syncs to {origin}. vaultlines only supports GitHub remotes.") from None
    cfg.vaults[name] = Vault(name, path, url)
    config.save(cfg)
    say(f"Adopted {contract(path)} as vault '{name}'" + (f", synced to {url}" if url else ", on this computer only"))
    _apply(cfg)


def cmd_vault_remove(args) -> None:
    cfg = config.load()
    vault = cfg.vault(args.name)
    for name, _, settings in plugins.sources(cfg):
        if settings["vault"] == vault.name:
            raise VlError(f"Plugin '{name}' fills '{vault.name}'. "
                          f"Remove [plugins.{name}] from {contract(config.config_path())} first.")
    used = cfg.users(vault.name)
    if used:
        raise VlError(f"These folders still use '{vault.name}': {', '.join(show(f) for f in sorted(used))}. "
                      "Change them with `vl folder set` or `vl folder unset` first.")
    del cfg.vaults[vault.name]
    for _, plugin, settings in plugins.configured(cfg):
        if hasattr(plugin, "vault_removed"):
            plugin.vault_removed(cfg, settings, vault.name)
    config.save(cfg)
    if args.delete_files:
        shutil.rmtree(vault.path)
        say(f"Deleted {contract(vault.path)}")
    else:
        say(f"Removed vault '{vault.name}'. Its files are still at {contract(vault.path)}")
    _apply(cfg)


# ---------------------------------------------------------------- folders

def _describe(f: Folder) -> str:
    text = f"{show(f.path)} writes to {f.writes}"
    if f.reads:
        text += f" and reads {', '.join(f.reads)} (writing there asks first)"
    return text + ("; pulled on every sync" if f.auto_pull else "")


def cmd_folder_set(args) -> None:
    cfg = config.load()
    if args.path == "*":
        raise VlError('There is no "*" any more. Use "~" to cover every folder in your home folder.')
    path = expand(args.path)
    if not path.is_dir():
        raise VlError(f"Folder not found: {args.path}")
    folder = str(path)
    reads = list(dict.fromkeys(r.strip() for r in (args.reads or "").split(",") if r.strip()))
    if args.auto_pull and not gitsync.in_work_tree(path):
        raise VlError(f"{contract(folder)} isn't in a git repo, so it can't use --auto-pull.")
    cfg.folders[folder] = Folder(folder, args.writes, reads, args.auto_pull)
    config.validate(cfg)
    config.save(cfg)
    say(_describe(cfg.folders[folder]))
    _apply(cfg)
    auds, _ = audience.audiences(cfg, fresh=False)
    for line in preview(cfg.folders[folder], auds, audience.cached_me()):
        say(f"  {line}")


def cmd_folder_unset(args) -> None:
    cfg = config.load()
    folder = str(expand(args.path))
    if folder not in cfg.folders:
        raise VlError(f"{show(folder)} isn't listed in the config.")
    del cfg.folders[folder]
    config.save(cfg)
    parent = folder_for(cfg, folder)
    say(f"{show(folder)} is no longer listed" +
        (f"; it uses {show(parent.path)} now." if parent else "; it has no vaults now."))
    _apply(cfg)


def preview(f: Folder, auds: dict[str, Audience], me: str) -> list[str]:
    """Where writes in a folder will ask, given who can see each vault."""
    def aud(name: str) -> dict:
        a = auds.get(name) or audience.unknown("not checked yet")
        return {"kind": a.kind, "logins": list(a.logins), "reason": a.reason}

    lines = []
    for r in f.vaults:
        if r == f.writes:
            continue
        people = lbl.new_people(lbl.narrow(lbl.EVERYONE, aud(r)), aud(f.writes), me)
        if people:
            lines.append(f"writes to {f.writes} ask after reading {r} ({lbl.names(people)} can't see {r})")
    for r in f.reads:
        lines.append(f"writes to {r} always ask (it's in reads)")
    return lines


# ---------------------------------------------------------------- apply

@dataclass
class Applied:
    errors: list[str] = field(default_factory=list)  # vaults GitHub couldn't be asked about


def _remove_v2(cfg: Config, warnings: list[str]) -> None:
    """Remove the vl-* servers and mcp__vl-* rules vaultlines 0.2 added."""
    listed = {f.path for f in cfg.listed()}
    for name in claude.user_servers():
        if name.startswith(claude.V2_PREFIX):
            claude.remove_server(name, "user")
    for folder, names in claude.v2_folder_servers().items():
        if not Path(folder).is_dir():
            warnings.append(f"{contract(folder)} is gone but still has vl servers in Claude Code")
            continue
        for name in names:
            claude.remove_server(name, "local", cwd=folder)
        if folder not in listed:
            claude.update_settings(Path(folder) / ".claude" / "settings.local.json", None)
    user = claude.user_settings_path()
    claude.update_settings(user, read_json(user, {}).get("basicMemory"))  # only strips old rules


def write_runtime(cfg: Config, fresh: bool) -> tuple[dict[str, Audience], list[str]]:
    auds, errors = audience.audiences(cfg, fresh=fresh)
    data = {name: plugin.data(cfg, settings) if hasattr(plugin, "data") else {}
            for name, plugin, settings in plugins.configured(cfg)}
    runtime.write(runtime.build(cfg, auds, audience.cached_me(), data))
    return auds, errors


def _apply(cfg: Config, fresh: bool = True) -> Applied:
    """Make git, the plugins, Claude Code and Obsidian match the config."""
    warnings: list[str] = []
    for v in cfg.vaults.values():
        if not v.path.exists():
            warnings.append(f"vault '{v.name}': folder {contract(v.path)} is missing")
            continue
        if not gitsync.is_repo(v.path):
            gitsync.init_repo(v.path)

    _remove_v2(cfg, warnings)
    saved = audience.load_state().get("plugins", {})
    plugin_state = {}
    for name, plugin, settings in plugins.configured(cfg):
        before = saved.get(name, {})
        if before.get("kind") != settings["kind"]:
            _plugin_off(name, before, warnings)
            before = {}
        new = plugin.apply(cfg, settings, before.get("state", {}), warnings) if hasattr(plugin, "apply") else None
        plugin_state[name] = {"kind": settings["kind"], "state": new or {}}
    for name, before in saved.items():
        if name not in cfg.plugins:
            _plugin_off(name, before, warnings)
    claude.install_hooks()

    _, errors = write_runtime(cfg, fresh)
    state = audience.load_state()
    state.pop("plugin_blocks", None)  # kept by vl 0.3.0, before plugins
    state["plugins"] = plugin_state
    if fresh and not errors:
        state["checked_at"] = time.time()
    audience.save_state(state)

    for f in cfg.listed():
        if f.auto_pull and Path(f.path).is_dir() and not gitsync.in_work_tree(Path(f.path)):
            warnings.append(f"{contract(f.path)} has auto_pull but isn't in a git repo")
    warnings += hook_warnings(cfg)

    todo = obsidian.register([v.path for v in cfg.vaults.values() if v.path.exists()])
    if todo:
        say("Obsidian is open, so these vaults weren't added to it. Close Obsidian and run `vl apply`:")
        for p in todo:
            say(f"  {contract(p)}")
    for w in warnings:
        warn(w)
    for e in errors:
        warn(e)
    return Applied(errors)


def _plugin_off(name: str, saved: dict, warnings: list[str]) -> None:
    """Undo what a plugin set up, once it's gone from the config."""
    plugin = plugins.KINDS.get(saved.get("kind"))
    if plugin and hasattr(plugin, "off"):
        plugin.off(saved.get("state", {}), warnings)


def cmd_apply(args) -> None:
    result = _apply(config.load())
    say("Applied." if not result.errors else "Applied, with the warnings above.")


def hook_warnings(cfg: Config) -> list[str]:
    """Listed folders where a settings file turns every hook off."""
    out = []
    seen = set()
    for f in cfg.listed():
        if not Path(f.path).is_dir():
            continue
        for path in claude.disables_hooks(f.path):
            if path not in seen:
                seen.add(path)
                out.append(f"{contract(path)} sets disableAllHooks, so vl can't guard vaults in sessions there")
    return out


# ---------------------------------------------------------------- init

REQUIRED = {
    "git": "https://git-scm.com (on macOS: xcode-select --install)",
    "claude": "https://claude.com/claude-code",
}


def cmd_init(args) -> None:
    required = dict(REQUIRED)
    if not args.no_basic_memory:
        required["uvx"] = "https://docs.astral.sh/uv/ (on macOS: brew install uv)"
    if not args.local:
        required["gh"] = "https://cli.github.com (or use --local for a personal vault on this computer only)"
    missing = [f"  {tool}: {hint}" for tool, hint in required.items() if not shutil.which(tool)]
    if missing:
        raise VlError("Install these first:\n" + "\n".join(missing))
    new = not config.config_path().exists()
    cfg = config.load()
    if args.interval:
        cfg.sync_interval = args.interval
    if args.no_basic_memory:
        cfg.plugins = {name: p for name, p in cfg.plugins.items() if p["kind"] != "basic-memory"}
    elif new:
        cfg.plugins["basic-memory"] = {"kind": "basic-memory"}

    for _, plugin, settings in plugins.configured(cfg):
        if hasattr(plugin, "init"):
            plugin.init(cfg, settings)

    home_key = str(expand("~"))
    if home_key not in cfg.folders:
        if "personal" not in cfg.vaults:
            create_vault(cfg, "personal", None, args.local, None)
        cfg.folders[home_key] = Folder(home_key, "personal")
    config.save(cfg)
    _apply(cfg)
    say("Claude Code hooks installed: every session is guarded by vl.")

    if launchd.supported():
        launchd.install(cfg.sync_interval)
        say(f"Sync runs every {cfg.sync_interval // 60} min. Log: {contract(launchd.log_path())}")
    elif sys.platform != "darwin":
        say(f"No background sync on this system yet. Add a cron job: */10 * * * * {shutil.which('vl') or 'vl'} sync --background")
    say("\nDone. Next: `vl vault create`, `vl vault join`, or `vl folder set`. See `vl status`.")


def cmd_uninstall(args) -> None:
    claude.remove_hooks()
    launchd.uninstall()
    say("Removed vl's Claude Code hooks and stopped background sync. Vaults, settings and notes are untouched.")


# ---------------------------------------------------------------- sync

def _daily_check(cfg: Config, stamp, background: bool) -> None:
    """Ask GitHub again, rewrite runtime.json, and report new disableAllHooks files."""
    _, errors = write_runtime(cfg, fresh=True)
    state = audience.load_state()
    if not errors:
        state["checked_at"] = time.time()
    before = set(state.get("hook_warnings", []))
    now = hook_warnings(cfg)
    state["hook_warnings"] = now
    audience.save_state(state)
    new = [w for w in now if w not in before]
    for w in new:
        say(f"{stamp()}check: {w}")
    if new and background:
        notify(f"vl can't guard some folders: {len(new)} settings file(s) set disableAllHooks. Run `vl check`.")
    if errors:
        say(f"{stamp()}check: couldn't reach GitHub for every vault; trying again next sync")


def _run_source(cfg: Config, name: str, plugin, settings: dict, vault: Vault,
                rebuild: bool = False, take_over: bool = False) -> tuple[str, bool]:
    """Fill a vault from its source and commit what changed, apart from notes. (status, changed)"""
    if not gitsync.is_repo(vault.path):
        raise VlError(f"{contract(vault.path)} is not a git repo. Run `vl apply` to set it up.")
    # First get the vault's latest, so a computer that took over is seen.
    gitsync.sync(vault.path)
    plugins.claim(vault.path, vault.name, name, take_over)
    try:
        status = plugin.run(cfg, settings, vault, rebuild=rebuild)
    except VlError:
        gitsync.commit(vault.path, f"Partial update from {name}")  # what did arrive stays apart from notes
        raise
    changed = gitsync.commit(vault.path, f"Update from {name}")
    return (status if changed else "no changes"), changed


def _source_for(cfg: Config, vault: str) -> tuple[str, object, dict]:
    """The source that fills a vault on this computer."""
    for name, plugin, settings in plugins.sources(cfg):
        if settings["vault"] == vault:
            return name, plugin, settings
    raise VlError(f"No source fills '{vault}' on this computer.")


def _run_sources(cfg: Config, only: str | None, stamp, background: bool,
                 rebuild: bool = False, take_over: bool = False) -> list[str]:
    """Run the sources that are due, or the one that fills `only` now. Returns the vaults that failed."""
    state = audience.load_state()
    runs = state.setdefault("runs", {})
    failed = []
    for name, plugin, settings in plugins.sources(cfg):
        vault = cfg.vaults[settings["vault"]]
        now = time.time()
        if only is not None and vault.name != only:
            continue
        last = runs.get(name) or {}
        # A failed run is tried again on the next sync, as a failed vault sync is.
        if only is None and not plugins.due(last.get("at") if last.get("ok") else None, plugins.every(plugin, settings), now):
            continue
        try:
            status, changed = _run_source(cfg, name, plugin, settings, vault, rebuild, take_over)
            runs[name] = {"at": now, "ok": True, "status": status}
            if not background or changed:
                say(f"{stamp()}{name} -> {vault.name}: {status}")
        except VlError as e:
            failed.append(vault.name)
            runs[name] = {"at": now, "ok": False, "status": str(e).splitlines()[-1].strip()}
            say(f"{stamp()}{name} -> {vault.name}: FAILED: {e}")
    audience.save_state(state)
    return failed


def clean_fetched(max_age: float = 86400) -> int:
    """Delete originals `vl fetch` got more than `max_age` seconds ago. Returns how many."""
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


def cmd_sync(args) -> None:
    cfg = config.load()
    if args.rebuild or args.take_over:
        if not args.vault:
            raise VlError("--rebuild and --take-over need the VAULT a source fills, like `vl sync drive --rebuild`.")
        _source_for(cfg, cfg.vault(args.vault).name)
    lock_file = config.config_dir() / "sync.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("Another sync is running.")
            return
        stamp = (lambda: time.strftime("%Y-%m-%d %H:%M:%S ")) if args.background else (lambda: "")
        if not args.vault and time.time() - audience.load_state().get("checked_at", 0) >= cfg.check_interval:
            _daily_check(cfg, stamp, args.background)
        lbl.clean_sessions(state_dir())
        clean_fetched()
        vaults = [cfg.vault(args.vault)] if args.vault else list(cfg.vaults.values())
        # Before the vaults, so a source's commit goes out in the same sync.
        failed_sources = _run_sources(cfg, args.vault, stamp, args.background, args.rebuild, args.take_over)
        failed = []
        for v in vaults:
            try:
                status = gitsync.sync(v.path)
                if not args.background or status != "synced":
                    say(f"{stamp()}{v.name}: {status}")
            except VlError as e:
                failed.append(v.name)
                say(f"{stamp()}{v.name}: FAILED: {e}")
        if not args.vault:
            for f in cfg.listed():
                if not f.auto_pull:
                    continue
                try:
                    gitsync.pull_only(Path(f.path))
                    if not args.background:
                        say(f"{contract(f.path)}: pulled")
                except VlError as e:
                    say(f"{stamp()}{contract(f.path)}: pull skipped: {str(e).splitlines()[-1]}")
        if args.background and failed_sources:
            notify(f"Couldn't update {', '.join(failed_sources)} from its source. "
                   f"Run `vl sync {failed_sources[0]}` to see why.")
        if args.background and failed:
            notify(f"Couldn't sync: {', '.join(failed)}. Run `vl sync` to see why.")
        if failed or failed_sources:
            sys.exit(1)


# ---------------------------------------------------------------- check / status / sessions / doctor

def _folder_rows(cfg: Config) -> list[tuple[str, ...]]:
    rows = [("folder", "writes", "reads", "auto_pull")]
    for f in cfg.listed():
        rows.append((show(f.path), f.writes, ", ".join(f.reads) or "-", "yes" if f.auto_pull else ""))
    return rows


def cmd_check(args) -> None:
    cfg = config.load()
    auds, errors = write_runtime(cfg, fresh=True)
    state = audience.load_state()
    if not errors:
        state["checked_at"] = time.time()
    state["hook_warnings"] = hook_warnings(cfg)
    audience.save_state(state)
    me = audience.cached_me()
    say("Who can see each vault")
    _table([("vault", "who can see it")] + [(name, auds[name].describe()) for name in sorted(auds)])
    if cfg.folders:
        say("\nWhere writes will ask")
        width = max(len(show(f.path)) for f in cfg.listed()) + 1
        for f in cfg.listed():
            lines = preview(f, auds, me) or ["never"]
            say(f"  {(show(f.path) + ':').ljust(width)}  {lines[0]}")
            for line in lines[1:]:
                say(f"  {' ' * width}  {line}")
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


def _source_rows(cfg: Config) -> list[tuple[str, ...]]:
    runs = audience.load_state().get("runs", {})
    rows = [("source", "kind", "vault", "every", "computer", "last run")]
    for name, plugin, settings in plugins.sources(cfg):
        every = plugins.every(plugin, settings)
        host = (plugins.owner(cfg.vaults[settings["vault"]].path) or {}).get("host") or "-"
        run = runs.get(name)
        if not run:
            last = "never"
        elif run["ok"]:
            last = f"{_ago(time.time() - run['at'])}, {run['status']}"
        else:
            last = f"FAILED {_ago(time.time() - run['at'])}: {run['status']}"
        rows.append((name, settings["kind"], settings["vault"], f"{every // 60} min" if every % 60 == 0 else f"{every} s",
                     host, last))
    return rows


def cmd_status(args) -> None:
    cfg = config.load()
    if not cfg.vaults:
        say("No vaults yet. Run `vl init`.")
        return
    auds, _ = audience.audiences(cfg, fresh=False)
    say("Vaults")
    rows = [("vault", "path", "remote", "state", "who can see it")]
    for v in sorted(cfg.vaults.values(), key=lambda v: v.name):
        if v.path.exists() and gitsync.is_repo(v.path):
            pending = gitsync.pending_changes(v.path)
            state = f"last commit {gitsync.last_commit_age(v.path)}" + (f", {pending} unsaved" if pending else "")
        else:
            state = "MISSING"
        remote = (v.remote or "-").replace("https://github.com/", "github:").removesuffix(".git")
        rows.append((v.name, contract(v.path), remote, state, auds[v.name].describe()))
    _table(rows)
    say("\nFolders")
    if cfg.folders:
        _table(_folder_rows(cfg))
    else:
        say('  none. Add one with `vl folder set ~ --writes VAULT`.')
    if plugins.sources(cfg):
        say("\nSources")
        _table(_source_rows(cfg))
    say("")
    hooks = claude.hooks_installed()
    say(f"Claude Code hooks: {'installed' if len(hooks) == len(claude.HOOK_EVENTS) else 'MISSING (run `vl apply`)'}")
    problem = runtime.stale(runtime.load())
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
    rows = [("updated", "session", "folder", "label", "read")]
    for sid, s in sessions:
        rows.append((
            time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("updated", 0))),
            sid[:8],
            show(s["folder"]) if s.get("folder") else "(no vaults)",
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
    needs_gh = any(v.remote for v in cfg.vaults.values())
    for tool in ("git", "claude", "gh"):
        found = shutil.which(tool)
        optional = tool == "gh" and not needs_gh
        check(bool(found) or optional, f"{tool}: {found or ('not found (not needed)' if optional else 'not found')}")
    say("Vaults")
    for v in cfg.vaults.values():
        check(v.path.exists() and gitsync.is_repo(v.path), f"vault '{v.name}' is a git repo", "vl apply")
        origin = gitsync.remote_url(v.path) if gitsync.is_repo(v.path) else None
        same = (origin is None and v.remote is None) or (
            origin is not None and v.remote is not None and gitsync.repo_key(origin) == gitsync.repo_key(v.remote))
        check(same, f"vault '{v.name}' syncs to the remote in the config ({v.remote or 'none'})",
              f"git remote is {origin or 'none'}; fix one of them")
    for _, plugin, settings in plugins.configured(cfg):
        if hasattr(plugin, "doctor"):
            plugin.doctor(cfg, settings, check)
    say("Claude Code")
    hooks = claude.hooks_installed()
    for event in claude.HOOK_EVENTS:
        check(event in hooks, f"{event} hook runs `vl hook`", "vl apply")
    check(not any(n.startswith(claude.V2_PREFIX) for n in claude.user_servers()), "no vaultlines 0.2 servers left", "vl apply")
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


# ---------------------------------------------------------------- sources

def cmd_fetch(args) -> None:
    cfg = config.load()
    vault = cfg.vault(args.vault)
    try:
        _, plugin, settings = _source_for(cfg, vault.name)
    except VlError:
        raise VlError(f"No source fills '{vault.name}' on this computer, so there's nothing to fetch from.") from None
    if not hasattr(plugin, "fetch"):
        raise VlError(f"The {settings['kind']} source can't fetch originals.")
    say(str(plugin.fetch(cfg, settings, vault, args.path, fetch_dir(vault.name))))


def _ask_shared_drive(drives: list[dict]) -> str | None:
    """Ask which shared drive to use. None: My Drive."""
    say("This account has shared drives. Which one should the vault hold?")
    say("  0  My Drive")
    for i, d in enumerate(drives, 1):
        say(f"  {i}  {d.get('name')}")
    while True:
        answer = input("Number [0]: ").strip() or "0"
        if answer.isdigit() and int(answer) <= len(drives):
            return drives[int(answer) - 1]["id"] if int(answer) else None
        say(f"Type a number from 0 to {len(drives)}.")


def cmd_source_add(args) -> None:
    from .plugins import drive

    cfg = config.load()
    name = args.name
    _check_new_name(cfg, name)
    if name in cfg.plugins:
        raise VlError(f"There's already a plugin named '{name}' in {contract(config.config_path())}.")
    if bool(args.client_id) != bool(args.client_secret):
        raise VlError("--client-id and --client-secret go together.")
    if args.github:
        _check_owner_repo(args.github)
    drive.require_tools()

    remote = args.remote
    if remote is None:
        if not args.client_id:
            warn("rclone's shared client ID is slow and is being retired during 2026. "
                 f"Make your own and pass --client-id and --client-secret: {drive.CLIENT_ID_HELP}")
        remote = f"vl-{name}:"
        say(f"Making the rclone remote {remote} with read-only access. Sign in to Google in the browser.")
        drive.make_remote(f"vl-{name}", args.client_id, args.client_secret)
    parsed = drive.parse_remote(remote)
    drive.check_read_only({"remote": remote})
    if parsed.name and "team_drive" not in parsed.overrides:
        drives = drive.shared_drives(parsed.name)
        if args.shared_drive:
            parsed.overrides["team_drive"] = drive.find_shared_drive(drives, args.shared_drive)
        elif drives and sys.stdin.isatty():
            team = _ask_shared_drive(drives)
            if team:
                parsed.overrides["team_drive"] = team
        elif drives:
            say("Using My Drive. Pass --shared-drive NAME for a shared drive.")
    elif args.shared_drive:
        raise VlError("--shared-drive needs a Drive remote without team_drive in it.")
    if args.folder:
        parsed.path = "/".join(p for p in (parsed.path.rstrip("/"), args.folder.strip("/")) if p)
    remote = drive.remote_string(parsed) if parsed.name else parsed.path

    settings = {"kind": "drive", "vault": name, "remote": remote}
    problem = drive.validate(settings)
    if problem:
        raise VlError(f"{problem[0]}: {problem[1]}")
    create_vault(cfg, name, args.github, args.local, None)
    cfg.plugins[name] = settings
    config.save(cfg)
    say(f"Added [plugins.{name}] to {contract(config.config_path())}, with remote = {remote!r}")
    _apply(cfg)
    say("Turning Drive files into notes. The first run can take a while.")
    cmd_sync(argparse.Namespace(vault=name, background=False, rebuild=False, take_over=False))
    say(f"\nDone. vl updates '{name}' every {drive.EVERY // 60} min, from this computer only.")
    say(f"Next: let Claude read it from a folder, like `vl folder set ~/code/app --writes VAULT --reads {name}`.")


def cmd_hook(args) -> None:
    from .hook import main as hook_main

    hook_main()


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vl", description="Personal and team markdown vaults for Claude Code.")
    p.add_argument("--version", action="version", version=f"vaultlines {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="set up this computer")
    s.add_argument("--local", action="store_true",
                   help="keep the personal vault on this computer only (default: a private GitHub repo)")
    s.add_argument("--no-basic-memory", action="store_true", help="don't set up Basic Memory")
    s.add_argument("--interval", type=int, help="seconds between background syncs (default 600)")
    s.set_defaults(func=cmd_init)

    vault = sub.add_parser("vault", help="create, join, adopt or remove vaults")
    vsub = vault.add_subparsers(dest="vault_command", required=True, metavar="ACTION")

    s = vsub.add_parser("create", help="create a new vault")
    s.add_argument("name")
    where = s.add_mutually_exclusive_group()
    where.add_argument("--github", metavar="OWNER/REPO", help="private GitHub repo to create (default YOU/vault-NAME)")
    where.add_argument("--local", action="store_true", help="no remote: this computer only")
    s.add_argument("--path", help="default: ~/Vaults/NAME")
    s.set_defaults(func=cmd_vault_create)

    s = vsub.add_parser("join", help="clone a vault from GitHub")
    s.add_argument("url", help="https://github.com/OWNER/REPO.git")
    s.add_argument("--name", help="default: the repo name")
    s.add_argument("--path", help="default: ~/Vaults/NAME")
    s.set_defaults(func=cmd_vault_join)

    s = vsub.add_parser("adopt", help="manage a folder that already has notes")
    s.add_argument("path")
    s.add_argument("--github", metavar="OWNER/REPO", help="also create a private GitHub repo for it")
    s.add_argument("--name", help="default: the folder name")
    s.set_defaults(func=cmd_vault_adopt)

    s = vsub.add_parser("remove", help="stop managing a vault (keeps files unless --delete-files)")
    s.add_argument("name")
    s.add_argument("--delete-files", action="store_true")
    s.set_defaults(func=cmd_vault_remove)

    s = vsub.add_parser("list", help="same as `vl status`")
    s.set_defaults(func=cmd_status)

    folder = sub.add_parser("folder", help="choose which vaults Claude uses in a folder")
    fsub = folder.add_subparsers(dest="folder_command", required=True, metavar="ACTION")

    s = fsub.add_parser("set", help="set a folder's vaults (covers its subfolders too)")
    s.add_argument("path", help='a folder; "~" covers your whole home folder')
    s.add_argument("--writes", required=True, metavar="VAULT", help="the vault Claude saves notes to here")
    s.add_argument("--reads", metavar="A,B", help="other vaults Claude may read here; writing to them asks first")
    s.add_argument("--auto-pull", action="store_true", help="git pull this folder on every sync (fast-forward only)")
    s.set_defaults(func=cmd_folder_set)

    s = fsub.add_parser("unset", help="remove a folder's entry (it then uses its closest listed parent)")
    s.add_argument("path")
    s.set_defaults(func=cmd_folder_unset)

    s = sub.add_parser("sync", help="sync vaults now (with VAULT, also run its source now)")
    s.add_argument("vault", nargs="?")
    s.add_argument("--rebuild", action="store_true", help="write every note of VAULT's source again")
    s.add_argument("--take-over", action="store_true", help="fill VAULT's source from this computer from now on")
    s.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("fetch", help="download the original of a file in a source's vault")
    s.add_argument("vault")
    s.add_argument("path", help="the `path` from the note's frontmatter")
    s.set_defaults(func=cmd_fetch)

    source = sub.add_parser("source", help="fill a vault from Google Drive")
    ssub = source.add_subparsers(dest="source_command", required=True, metavar="ACTION")
    s = ssub.add_parser("add", help="set up a read-only Drive remote, a vault and its source, and run it once")
    s.add_argument("name", help="the vault's name, and the source's")
    s.add_argument("--drive", action="store_true", required=True, help="from Google Drive (the only kind so far)")
    s.add_argument("--folder", metavar="PATH", help="only this folder of the drive")
    s.add_argument("--shared-drive", metavar="NAME_OR_ID", help="a shared drive (default: ask, or My Drive)")
    s.add_argument("--remote", metavar="EXISTING:", help="a read-only rclone remote you already have")
    s.add_argument("--client-id", help="your own Google OAuth client ID for rclone")
    s.add_argument("--client-secret", help="its secret")
    where = s.add_mutually_exclusive_group()
    where.add_argument("--github", metavar="OWNER/REPO", help="private GitHub repo to create (default YOU/vault-NAME)")
    where.add_argument("--local", action="store_true", help="no remote: this computer only")
    s.set_defaults(func=cmd_source_add)

    s = sub.add_parser("sessions", help="recent Claude sessions and what they read (for debugging)")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    sub.add_parser("check", help="ask GitHub who can see each vault, and show where writes will ask").set_defaults(func=cmd_check)
    sub.add_parser("apply", help="set up git, Basic Memory and Claude Code from the config").set_defaults(func=cmd_apply)
    sub.add_parser("status", help="show vaults, folders, hooks and sync").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="check that everything is set up").set_defaults(func=cmd_doctor)
    sub.add_parser("uninstall", help="remove the hooks and stop background sync").set_defaults(func=cmd_uninstall)
    sub.add_parser("hook", help=argparse.SUPPRESS).set_defaults(func=cmd_hook)
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
