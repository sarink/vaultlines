"""The `vl` command."""

from __future__ import annotations

import argparse
import fcntl
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, audience, bm, claude, config, gitsync, launchd, obsidian
from .audience import Audience, Problem
from .config import NAME_RE, STAR, Config, Folder, Vault, show
from .rules import admin_denies, folder_plan
from .util import VlError, contract, expand, notify, say, warn

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
    bm.ensure_project(cfg, name, vault_path)
    cfg.vaults[name] = Vault(name, vault_path, url)
    say(f"Created vault '{name}' at {contract(vault_path)}" + (f", synced to {url}" if url else ", on this computer only"))
    return cfg.vaults[name]


def cmd_vault_create(args) -> None:
    cfg = config.load()
    create_vault(cfg, args.name, args.github, args.local, args.path)
    config.save(cfg)
    _finish(cfg)


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
    bm.ensure_project(cfg, name, path)
    cfg.vaults[name] = Vault(name, path, args.url)
    config.save(cfg)
    say(f"Joined vault '{name}' at {contract(path)}")
    _finish(cfg)


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
    bm.ensure_project(cfg, name, path)
    cfg.vaults[name] = Vault(name, path, url)
    config.save(cfg)
    say(f"Adopted {contract(path)} as vault '{name}'" + (f", synced to {url}" if url else ", on this computer only"))
    _finish(cfg)


def cmd_vault_remove(args) -> None:
    cfg = config.load()
    vault = cfg.vault(args.name)
    used = cfg.users(vault.name)
    if used:
        raise VlError(f"These folders still use '{vault.name}': {', '.join(show(f) for f in sorted(used))}. "
                      "Change them with `vl folder set` or `vl folder unset` first.")
    del cfg.vaults[vault.name]
    bm.remove_project(cfg, vault.name)
    config.save(cfg)
    if args.delete_files:
        shutil.rmtree(vault.path)
        say(f"Deleted {contract(vault.path)}")
    else:
        say(f"Removed vault '{vault.name}'. Its files are still at {contract(vault.path)}")
    _finish(cfg)


# ---------------------------------------------------------------- folders

def _folder_arg(text: str) -> str:
    if text == STAR:
        return STAR
    path = expand(text)
    if not path.is_dir():
        raise VlError(f"Folder not found: {text}")
    return str(path)


def _describe(f: Folder) -> str:
    text = f"{show(f.path)} writes to {f.writes}"
    if f.reads:
        text += f" and reads {', '.join(f.reads)} (writing there asks first)"
    return text + ("; pulled on every sync" if f.auto_pull else "")


def cmd_folder_set(args) -> None:
    cfg = config.load()
    folder = _folder_arg(args.path)
    reads = list(dict.fromkeys(r.strip() for r in (args.reads or "").split(",") if r.strip()))
    if args.auto_pull and folder != STAR and not gitsync.in_work_tree(Path(folder)):
        raise VlError(f"{contract(folder)} isn't in a git repo, so it can't use --auto-pull.")
    cfg.folders[folder] = Folder(folder, args.writes, reads, args.auto_pull)
    config.validate(cfg)
    auds, errors = audience.audiences(cfg)
    problems = audience.folder_problems(cfg, folder, auds, audience.cached_me())
    if problems:
        raise VlError("\n".join(p.message() for p in problems) + "\nNothing changed.")
    config.save(cfg)
    say(_describe(cfg.folders[folder]))
    _finish(cfg, auds, errors)


def cmd_folder_unset(args) -> None:
    cfg = config.load()
    folder = STAR if args.path == STAR else str(expand(args.path))
    if folder not in cfg.folders:
        raise VlError(f"{show(folder)} isn't listed in the config.")
    del cfg.folders[folder]
    config.save(cfg)
    say(f"{show(folder)} is no longer listed" + ("." if folder == STAR else '; it uses "*" now.'))
    _finish(cfg)


# ---------------------------------------------------------------- apply

@dataclass
class Applied:
    refused: list[Problem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # vaults GitHub couldn't be asked about


def _apply(cfg: Config, auds: dict[str, Audience] | None = None, errors: list[str] | None = None) -> Applied:
    """Make Basic Memory, Claude Code and Obsidian match the config, for the folders that pass the check."""
    warnings = []

    for v in cfg.vaults.values():
        if not v.path.exists():
            warnings.append(f"vault '{v.name}': folder {contract(v.path)} is missing")
            continue
        if not gitsync.is_repo(v.path):
            gitsync.init_repo(v.path)
        bm.ensure_project(cfg, v.name, v.path)

    if auds is None:
        auds, errors = audience.audiences(cfg)
    errors = errors or []
    refused = audience.check(cfg, auds, audience.cached_me())
    refused_folders = {p.folder for p in refused}

    # User level: the "*" entry, available in every folder. If its reads are refused, only its write vault.
    user = folder_plan(cfg, STAR, drop_reads=STAR in refused_folders) if cfg.star else None
    wanted = {name: bm.mcp_argv(cfg, vault) for name, vault in (user.servers.items() if user else [])}
    current = claude.user_servers()
    for name in current.keys() - wanted.keys():
        claude.remove_server(name, "user")
    for name, argv in wanted.items():
        if name not in current or not claude.same_server(current[name], argv):
            claude.add_server(name, argv, "user")
    claude.update_settings(claude.user_settings_path(), primary=user.writes if user else None,
                           reads=user.reads if user else None, ask=user.ask if user else None,
                           deny=admin_denies(cfg))
    if user:
        bm.set_default(cfg, user.writes)

    # Listed folders. Refused ones aren't set up, so they fall back to "*".
    plans = {f.path: folder_plan(cfg, f.path) for f in cfg.listed() if f.path not in refused_folders}
    existing = claude.folder_servers()
    for folder in existing.keys() - plans.keys():
        if Path(folder).is_dir():
            for name in existing[folder]:
                claude.remove_server(name, "local", cwd=folder)
            claude.update_settings(Path(folder) / ".claude" / "settings.local.json", primary=None)
        else:
            warnings.append(f"{contract(folder)} is gone but still has vl servers in Claude Code")
    for folder, plan in plans.items():
        if not Path(folder).is_dir():
            warnings.append(f"folder {contract(folder)} is missing")
            continue
        have = existing.get(folder, {})
        for name in have.keys() - plan.servers.keys():
            claude.remove_server(name, "local", cwd=folder)
        for name, vault in plan.servers.items():
            argv = bm.mcp_argv(cfg, vault)
            if name not in have or not claude.same_server(have[name], argv):
                claude.add_server(name, argv, "local", cwd=folder)
        claude.update_settings(Path(folder) / ".claude" / "settings.local.json",
                               primary=plan.writes, reads=plan.reads, ask=plan.ask, deny=plan.deny)
        claude.git_ignore_local_settings(folder)
    for f in cfg.listed():
        if f.auto_pull and Path(f.path).is_dir() and not gitsync.in_work_tree(Path(f.path)):
            warnings.append(f"{contract(f.path)} has auto_pull but isn't in a git repo")

    todo = obsidian.register([v.path for v in cfg.vaults.values() if v.path.exists()])
    if todo:
        say("Obsidian is open, so these vaults weren't added to it. Close Obsidian and run `vl apply`:")
        for p in todo:
            say(f"  {contract(p)}")

    state = audience.load_state()
    state["refused"] = sorted(refused_folders)
    if not errors:
        state["checked_at"] = time.time()
    audience.save_state(state)

    for w in warnings:
        warn(w)
    for e in errors:
        warn(e)
    _print_refused(cfg, refused)
    return Applied(refused, errors)


def _print_refused(cfg: Config, refused: list[Problem]) -> None:
    for p in refused:
        print(f"error: {p.message()}", file=sys.stderr)
    for folder in dict.fromkeys(p.folder for p in refused):
        if folder == STAR:
            fallback = f'"*" gets only {cfg.star.writes} until this is fixed.'
        else:
            fallback = f'{show(folder)} isn\'t set up, so it uses "*" for now.'
        print(f"       {fallback}", file=sys.stderr)
    if refused:
        print("       Change the folder's reads, or who can see the vaults, then run `vl apply`.", file=sys.stderr)


def _finish(cfg: Config, auds: dict[str, Audience] | None = None, errors: list[str] | None = None) -> None:
    if _apply(cfg, auds, errors).refused:
        sys.exit(1)


def cmd_apply(args) -> None:
    result = _apply(config.load())
    if result.refused:
        sys.exit(1)
    say("Applied." if not result.errors else "Applied, with the warnings above.")


# ---------------------------------------------------------------- init

REQUIRED = {
    "git": "https://git-scm.com (on macOS: xcode-select --install)",
    "uvx": "https://docs.astral.sh/uv/ (on macOS: brew install uv)",
    "claude": "https://claude.com/claude-code",
}


def cmd_init(args) -> None:
    required = dict(REQUIRED)
    if not args.local:
        required["gh"] = "https://cli.github.com (or use --local for a personal vault on this computer only)"
    missing = [f"  {tool}: {hint}" for tool, hint in required.items() if not shutil.which(tool)]
    if missing:
        raise VlError("Install these first:\n" + "\n".join(missing))
    cfg = config.load()
    if args.interval:
        cfg.sync_interval = args.interval

    say("Configuring Basic Memory")
    bm.configure(cfg)
    if not claude.plugin_installed():
        say("Installing the Basic Memory plugin for Claude Code")
        claude.install_plugin()

    if not cfg.star:
        if "personal" not in cfg.vaults:
            create_vault(cfg, "personal", None, args.local, None)
        cfg.folders[STAR] = Folder(STAR, "personal", [])
    config.save(cfg)
    result = _apply(cfg)

    if launchd.supported():
        launchd.install(cfg.sync_interval)
        say(f"Sync runs every {cfg.sync_interval // 60} min. Log: {contract(launchd.log_path())}")
    elif sys.platform != "darwin":
        say(f"No background sync on this system yet. Add a cron job: */10 * * * * {shutil.which('vl') or 'vl'} sync --background")
    say("\nDone. Next: `vl vault create`, `vl vault join`, or `vl folder set`. See `vl status`.")
    if result.refused:
        sys.exit(1)


def cmd_uninstall(args) -> None:
    launchd.uninstall()
    say("Stopped background sync. Vaults, settings and notes are untouched.")


# ---------------------------------------------------------------- sync

def _daily_check(cfg: Config, stamp, background: bool) -> None:
    """Ask GitHub again, re-apply, and tell the user about folders that stopped passing."""
    before = set(audience.load_state().get("refused", []))
    result = _apply(cfg)
    new = [p for p in result.refused if p.folder not in before]
    for p in new:
        say(f"{stamp()}check: {p.message()}")
    if new and background:
        folders = ", ".join(dict.fromkeys(show(p.folder) for p in new))
        notify(f"No longer set up: {folders}. Who can see its vaults changed. Run `vl check`.")
    if result.errors:
        say(f"{stamp()}check: couldn't reach GitHub for every vault; trying again next sync")


def cmd_sync(args) -> None:
    cfg = config.load()
    lock_file = config.config_dir() / "sync.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("Another sync is running.")
            return
        stamp = (lambda: time.strftime("%Y-%m-%d %H:%M:%S ")) if args.background else (lambda: "")
        if not args.vault and time.time() - audience.load_state().get("checked_at", 0) >= audience.DAY:
            _daily_check(cfg, stamp, args.background)
        vaults = [cfg.vault(args.vault)] if args.vault else list(cfg.vaults.values())
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
        if failed:
            if args.background:
                notify(f"Couldn't sync: {', '.join(failed)}. Run `vl sync` to see why.")
            sys.exit(1)


# ---------------------------------------------------------------- check / status / doctor

def _folder_rows(cfg: Config, problems: list[Problem]) -> list[tuple[str, ...]]:
    refused = {p.folder for p in problems}
    rows = [("folder", "writes", "reads", "auto_pull", "check")]
    for path in sorted(cfg.folders, key=lambda p: (p != STAR, p)):
        f = cfg.folders[path]
        rows.append((show(path), f.writes, ", ".join(f.reads) or "-", "yes" if f.auto_pull else "",
                     "REFUSED" if path in refused else "ok"))
    return rows


def _print_problems(problems: list[Problem]) -> None:
    if problems:
        say("")
        for p in problems:
            say(f"  REFUSED  {p.message()}")


def cmd_check(args) -> None:
    cfg = config.load()
    auds, errors = audience.audiences(cfg)
    problems = audience.check(cfg, auds, audience.cached_me())
    say("Who can see each vault")
    _table([("vault", "who can see it")] + [(name, auds[name].describe()) for name in sorted(auds)])
    if cfg.folders:
        say("\nFolders")
        _table(_folder_rows(cfg, problems))
    _print_problems(problems)
    for e in errors:
        warn(e)
    if problems:
        sys.exit(1)


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
    problems = audience.check(cfg, auds, audience.cached_me())
    say("\nFolders")
    if cfg.folders:
        _table(_folder_rows(cfg, problems))
    else:
        say('  none. Add one with `vl folder set "*" --writes VAULT`.')
    _print_problems(problems)
    checked = audience.load_state().get("checked_at")
    say(f"\nLast checked with GitHub: {time.strftime('%Y-%m-%d %H:%M', time.localtime(checked)) if checked else 'never'}")
    if launchd.supported():
        say(f"Background sync: {'on' if launchd.loaded() else 'OFF (run `vl init`)'}, every {cfg.sync_interval // 60} min")


def cmd_doctor(args) -> None:
    cfg = config.load()
    ok = True

    def check(passed: bool, text: str, fix: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        say(f"  {'ok  ' if passed else 'FAIL'}  {text}" + ("" if passed or not fix else f"  -> {fix}"))

    say("Tools")
    needs_gh = any(v.remote for v in cfg.vaults.values())
    for tool in ("git", "uvx", "claude", "gh"):
        found = shutil.which(tool)
        optional = tool == "gh" and not needs_gh
        check(bool(found) or optional, f"{tool}: {found or ('not found (only needed for GitHub remotes)' if optional else 'not found')}")
    say("Basic Memory")
    check(bm.setting(cfg, "disable_permalinks").lower().endswith("true"), "permalinks off", "vl init")
    projects = bm.projects(cfg)
    for v in cfg.vaults.values():
        check(v.path.exists() and gitsync.is_repo(v.path), f"vault '{v.name}' is a git repo", "vl apply")
        check(projects.get(v.name) == v.path, f"vault '{v.name}' is a Basic Memory project", "vl apply")
        origin = gitsync.remote_url(v.path) if gitsync.is_repo(v.path) else None
        same = (origin is None and v.remote is None) or (
            origin is not None and v.remote is not None and gitsync.repo_key(origin) == gitsync.repo_key(v.remote))
        check(same, f"vault '{v.name}' syncs to the remote in the config ({v.remote or 'none'})",
              f"git remote is {origin or 'none'}; fix one of them")
    say("Claude Code")
    check(claude.plugin_installed(), "Basic Memory plugin installed", "vl init")
    refused = set(audience.load_state().get("refused", []))
    if cfg.star:
        wanted = set(folder_plan(cfg, STAR, drop_reads=STAR in refused).servers)
        check(set(claude.user_servers()) == wanted, f"{', '.join(sorted(wanted))} available everywhere", "vl apply")
    folders = claude.folder_servers()
    for f in cfg.listed():
        if f.path in refused:
            check(False, f"{contract(f.path)} passes the audience check", "vl check")
            continue
        wanted = set(folder_plan(cfg, f.path).servers)
        check(set(folders.get(f.path, {})) == wanted, f"{contract(f.path)} has the right servers", "vl apply")
    if launchd.supported():
        say("Sync")
        check(launchd.loaded(), "background sync is on", "vl init")
    say("\nAll good." if ok else "\nSome checks failed.")
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vl", description="Personal and team Basic Memory vaults for Claude Code.")
    p.add_argument("--version", action="version", version=f"vaultlines {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="set up this computer")
    s.add_argument("--local", action="store_true",
                   help="keep the personal vault on this computer only (default: a private GitHub repo)")
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

    folder = sub.add_parser("folder", help="choose which vaults a folder writes and reads")
    fsub = folder.add_subparsers(dest="folder_command", required=True, metavar="ACTION")

    s = fsub.add_parser("set", help="set a folder's vaults (replaces its entry)")
    s.add_argument("path", help='a folder, or "*" for every folder that isn\'t listed')
    s.add_argument("--writes", required=True, metavar="VAULT", help="the one vault this folder writes to")
    s.add_argument("--reads", metavar="A,B", help="other vaults it can read; writing to them asks first")
    s.add_argument("--auto-pull", action="store_true", help="git pull this folder on every sync (fast-forward only)")
    s.set_defaults(func=cmd_folder_set)

    s = fsub.add_parser("unset", help='remove a folder\'s entry (it then uses "*")')
    s.add_argument("path")
    s.set_defaults(func=cmd_folder_unset)

    s = sub.add_parser("sync", help="sync vaults now")
    s.add_argument("vault", nargs="?")
    s.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sync)

    sub.add_parser("check", help="ask GitHub who can see each vault, and check every folder").set_defaults(func=cmd_check)
    sub.add_parser("apply", help="rewrite Claude Code settings from the config").set_defaults(func=cmd_apply)
    sub.add_parser("status", help="show vaults, folders and sync").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="check that everything is set up").set_defaults(func=cmd_doctor)
    sub.add_parser("uninstall", help="stop background sync").set_defaults(func=cmd_uninstall)
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
