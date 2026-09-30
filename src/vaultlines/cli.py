"""The `vl` command."""

from __future__ import annotations

import argparse
import fcntl
import re
import shutil
import sys
import time
from pathlib import Path

from . import __version__, bm, claude, config, gitsync, launchd, obsidian
from .config import LEVELS, Config, Vault
from .rules import admin_denies, folder_plan, readable, server_name
from .util import VlError, contract, expand, notify, run, say, warn

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ---------------------------------------------------------------- vaults

def _check_new_name(cfg: Config, name: str) -> None:
    if not NAME_RE.match(name):
        raise VlError("Vault names use lowercase letters, digits and dashes (e.g. acme-public).")
    if name in cfg.vaults:
        raise VlError(f"A vault named '{name}' already exists.")


def _check_team(level: str, team: str | None) -> None:
    if level != "personal" and not team:
        raise VlError(f"A {level} vault belongs to a team. Add --team NAME.")
    if level == "personal" and team:
        raise VlError("Personal vaults don't belong to a team. Drop --team.")


def _add_vault(cfg: Config, vault: Vault) -> None:
    bm.ensure_project(cfg, vault.name, vault.path)
    cfg.vaults[vault.name] = vault
    if vault.level == "personal" and not cfg.default_vault:
        cfg.default_vault = vault.name


def create_vault(cfg: Config, name: str, level: str, team: str | None,
                 remote: str | None, github: str | None, path: str | None) -> Vault:
    _check_new_name(cfg, name)
    _check_team(level, team)
    vault_path = expand(path) if path else cfg.vaults_dir / name
    if vault_path.exists() and any(vault_path.iterdir()) and not gitsync.is_repo(vault_path):
        raise VlError(f"{contract(vault_path)} already has files. Use `vl vault adopt` instead.")
    gitsync.init_repo(vault_path)

    choice = "github" if github else (remote or "github")
    url = None
    if choice == "github":
        owner_repo = github or (f"{cfg.github_owner}/vault-{name}" if cfg.github_owner else None)
        if not owner_repo:
            raise VlError("No GitHub account known. Pass --github OWNER/REPO, or --remote none.")
        say(f"Creating private GitHub repo {owner_repo}")
        url = gitsync.create_github_repo(vault_path, owner_repo)
    elif choice != "none":
        url = choice
        gitsync.set_remote(vault_path, url)
        gitsync.sync(vault_path)

    vault = Vault(name, vault_path, level, team, url)
    _add_vault(cfg, vault)
    say(f"Created {level} vault '{name}' at {contract(vault_path)}")
    return vault


def cmd_vault_create(args) -> None:
    cfg = config.load()
    create_vault(cfg, args.name, args.level, args.team, args.remote, args.github, args.path)
    config.save(cfg)
    _apply(cfg)


def cmd_vault_join(args) -> None:
    cfg = config.load()
    name = args.name or re.sub(r"\.git$", "", args.url.rstrip("/").split("/")[-1])
    _check_new_name(cfg, name)
    _check_team(args.level, args.team)
    path = expand(args.path) if args.path else cfg.vaults_dir / name
    say(f"Cloning {args.url}")
    gitsync.clone(args.url, path)
    gitsync.ensure_identity(path)
    _add_vault(cfg, Vault(name, path, args.level, args.team, args.url))
    config.save(cfg)
    say(f"Joined {args.level} vault '{name}' at {contract(path)}")
    _apply(cfg)


def cmd_vault_adopt(args) -> None:
    cfg = config.load()
    path = expand(args.path)
    if not path.is_dir():
        raise VlError(f"Folder not found: {args.path}")
    name = args.name or path.name
    _check_new_name(cfg, name)
    _check_team(args.level, args.team)
    if not gitsync.is_repo(path):
        gitsync.init_repo(path)
    gitsync.ensure_identity(path)
    _add_vault(cfg, Vault(name, path, args.level, args.team, gitsync.remote_url(path)))
    config.save(cfg)
    say(f"Adopted {contract(path)} as {args.level} vault '{name}'")
    _apply(cfg)


def cmd_vault_remove(args) -> None:
    cfg = config.load()
    vault = cfg.vault(args.name)
    for folder in [f for f, v in cfg.bindings.items() if v == vault.name]:
        del cfg.bindings[folder]
        say(f"Unbound {contract(folder)}")
    del cfg.vaults[vault.name]
    if cfg.default_vault == vault.name:
        cfg.default_vault = next((v.name for v in cfg.vaults.values() if v.level == "personal"), None)
    bm.remove_project(cfg, vault.name)
    config.save(cfg)
    _apply(cfg)
    if args.delete_files:
        shutil.rmtree(vault.path)
        say(f"Deleted {contract(vault.path)}")
    else:
        say(f"Removed vault '{vault.name}'. Its files are still at {contract(vault.path)}")


# ---------------------------------------------------------------- bindings

def cmd_bind(args) -> None:
    cfg = config.load()
    vault = cfg.vault(args.vault)
    folder = expand(args.folder or ".")
    if not folder.is_dir():
        raise VlError(f"Folder not found: {args.folder}")
    cfg.bindings[str(folder)] = vault.name
    config.save(cfg)
    _apply(cfg)
    plan = folder_plan(cfg, str(folder), vault.name)
    reads = f"; can also read {', '.join(plan.reads)} (writes there ask first)" if plan.reads else ""
    say(f"Bound {contract(folder)} to '{vault.name}'{reads}")


def cmd_unbind(args) -> None:
    cfg = config.load()
    folder = str(expand(args.folder or "."))
    if folder not in cfg.bindings:
        raise VlError(f"{contract(folder)} isn't bound to a vault.")
    del cfg.bindings[folder]
    config.save(cfg)
    _apply(cfg)
    say(f"Unbound {contract(folder)}")


def cmd_follow(args) -> None:
    cfg = config.load()
    path = str(expand(args.path))
    if not gitsync.is_repo(Path(path)):
        raise VlError(f"{args.path} isn't a git repo.")
    if path not in cfg.follow:
        cfg.follow.append(path)
        config.save(cfg)
    say(f"{contract(path)} will be kept up to date (pull only) on every sync")


def cmd_unfollow(args) -> None:
    cfg = config.load()
    path = str(expand(args.path))
    if path in cfg.follow:
        cfg.follow.remove(path)
        config.save(cfg)
    say(f"Stopped following {contract(path)}")


# ---------------------------------------------------------------- apply

def _apply(cfg: Config) -> list[str]:
    """Make Basic Memory, Claude Code and Obsidian match the config."""
    problems = []

    for v in cfg.vaults.values():
        if not v.path.exists():
            problems.append(f"vault '{v.name}': folder {contract(v.path)} is missing")
            continue
        if not gitsync.is_repo(v.path):
            gitsync.init_repo(v.path)
        bm.ensure_project(cfg, v.name, v.path)
    if cfg.default_vault:
        bm.set_default(cfg, cfg.default_vault)

    # User level: the default personal vault, available in every folder.
    wanted = {server_name(cfg.default_vault): bm.mcp_argv(cfg, cfg.default_vault)} if cfg.default_vault else {}
    current = claude.user_servers()
    for name in current.keys() - wanted.keys():
        claude.remove_server(name, "user")
    for name, argv in wanted.items():
        if name not in current or not claude.same_server(current[name], argv):
            claude.add_server(name, argv, "user")
    claude.update_settings(claude.user_settings_path(), primary=cfg.default_vault, deny=admin_denies(cfg))

    # Bound folders.
    plans = {folder: folder_plan(cfg, folder, vault) for folder, vault in cfg.bindings.items()}
    existing = claude.folder_servers()
    for folder in existing.keys() - plans.keys():
        if Path(folder).is_dir():
            for name in existing[folder]:
                claude.remove_server(name, "local", cwd=folder)
            claude.update_settings(Path(folder) / ".claude" / "settings.local.json", primary=None)
        else:
            problems.append(f"{contract(folder)} is gone but still has vl servers in Claude Code")
    for folder, plan in plans.items():
        if not Path(folder).is_dir():
            problems.append(f"bound folder {contract(folder)} is missing")
            continue
        have = existing.get(folder, {})
        for name in have.keys() - plan.servers.keys():
            claude.remove_server(name, "local", cwd=folder)
        for name, vault in plan.servers.items():
            argv = bm.mcp_argv(cfg, vault)
            if name not in have or not claude.same_server(have[name], argv):
                claude.add_server(name, argv, "local", cwd=folder)
        claude.update_settings(Path(folder) / ".claude" / "settings.local.json",
                               primary=plan.vault, reads=plan.reads, ask=plan.ask, deny=plan.deny)
        claude.git_ignore_local_settings(folder)

    todo = obsidian.register([v.path for v in cfg.vaults.values() if v.path.exists()])
    if todo:
        say("Obsidian is open, so these vaults weren't added to it. Close Obsidian and run `vl apply`:")
        for p in todo:
            say(f"  {contract(p)}")

    for p in problems:
        warn(p)
    return problems


def cmd_apply(args) -> None:
    problems = _apply(config.load())
    say("Applied." if not problems else "Applied, with the warnings above.")


# ---------------------------------------------------------------- init

REQUIRED = {
    "git": "https://git-scm.com (on macOS: xcode-select --install)",
    "uvx": "https://docs.astral.sh/uv/ (on macOS: brew install uv)",
    "claude": "https://claude.com/claude-code",
}


def cmd_init(args) -> None:
    missing = [f"  {tool}: {hint}" for tool, hint in REQUIRED.items() if not shutil.which(tool)]
    if missing:
        raise VlError("Install these first:\n" + "\n".join(missing))
    cfg = config.load()
    if args.interval:
        cfg.sync_interval = args.interval
    if not cfg.github_owner and shutil.which("gh"):
        cfg.github_owner = run(["gh", "api", "user", "--jq", ".login"], check=False).stdout.strip() or None
    config.save(cfg)

    say("Configuring Basic Memory")
    bm.configure(cfg)
    if not claude.plugin_installed():
        say("Installing the Basic Memory plugin for Claude Code")
        claude.install_plugin()

    if not args.no_personal and not any(v.level == "personal" for v in cfg.vaults.values()):
        create_vault(cfg, "personal", "personal", None, args.personal_remote, None, None)
    config.save(cfg)
    _apply(cfg)

    if launchd.supported():
        launchd.install(cfg.sync_interval)
        say(f"Sync runs every {cfg.sync_interval // 60} min. Log: {contract(launchd.log_path())}")
    elif sys.platform != "darwin":
        say(f"No background sync on this system yet. Add a cron job: */10 * * * * {shutil.which('vl') or 'vl'} sync --background")
    say("\nDone. Next: `vl vault create`, `vl vault join`, or `vl bind`. See `vl status`.")


def cmd_uninstall(args) -> None:
    launchd.uninstall()
    say("Stopped background sync. Vaults, settings and notes are untouched.")


# ---------------------------------------------------------------- sync

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
            for repo in cfg.follow:
                try:
                    gitsync.pull_only(Path(repo))
                except VlError as e:
                    say(f"{stamp()}{contract(repo)}: update skipped: {str(e).splitlines()[-1]}")
        if failed:
            if args.background:
                notify(f"Couldn't sync: {', '.join(failed)}. Run `vl sync` to see why.")
            sys.exit(1)


# ---------------------------------------------------------------- status / doctor

def cmd_status(args) -> None:
    cfg = config.load()
    if not cfg.vaults:
        say("No vaults yet. Run `vl init`.")
        return
    say("Vaults")
    rows = []
    for v in sorted(cfg.vaults.values(), key=lambda v: (LEVELS.index(v.level), v.name)):
        default = " (default)" if v.name == cfg.default_vault else ""
        level = f"{v.level}:{v.team}" if v.team else v.level
        if v.path.exists() and gitsync.is_repo(v.path):
            pending = gitsync.pending_changes(v.path)
            state = f"last commit {gitsync.last_commit_age(v.path)}" + (f", {pending} unsaved" if pending else "")
        else:
            state = "MISSING"
        remote = (v.remote or "no remote").replace("https://github.com/", "github:").removesuffix(".git")
        rows.append((v.name + default, level, contract(v.path), remote, state))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        say("  " + "  ".join(r[i].ljust(widths[i]) for i in range(4)) + "  " + r[4])

    say("\nFolders")
    say(f"  (everywhere else)  -> {cfg.default_vault or 'no vault'}")
    for folder, vault in sorted(cfg.bindings.items()):
        reads = readable(cfg, vault) if vault in cfg.vaults else []
        extra = f"  (also reads {', '.join(reads)})" if reads else ""
        say(f"  {contract(folder)}  -> {vault}{extra}")
    if cfg.follow:
        say("\nFollowed repos (pull only)")
        for repo in cfg.follow:
            say(f"  {contract(repo)}")
    if launchd.supported():
        say(f"\nBackground sync: {'on' if launchd.loaded() else 'OFF (run `vl init`)'}, every {cfg.sync_interval // 60} min")


def cmd_doctor(args) -> None:
    cfg = config.load()
    ok = True

    def check(passed: bool, text: str, fix: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        say(f"  {'ok  ' if passed else 'FAIL'}  {text}" + ("" if passed or not fix else f"  -> {fix}"))

    say("Tools")
    for tool in ("git", "uvx", "claude", "gh"):
        found = shutil.which(tool)
        check(bool(found) or tool == "gh", f"{tool}: {found or 'not found (only needed for GitHub remotes)'}")
    say("Basic Memory")
    check(bm.setting(cfg, "disable_permalinks").lower().endswith("true"), "permalinks off", "vl init")
    projects = bm.projects(cfg)
    for v in cfg.vaults.values():
        check(v.path.exists() and gitsync.is_repo(v.path), f"vault '{v.name}' is a git repo", "vl apply")
        check(projects.get(v.name) == v.path, f"vault '{v.name}' is a Basic Memory project", "vl apply")
    say("Claude Code")
    check(claude.plugin_installed(), "Basic Memory plugin installed", "vl init")
    user = claude.user_servers()
    if cfg.default_vault:
        check(server_name(cfg.default_vault) in user, f"{server_name(cfg.default_vault)} available everywhere", "vl apply")
    folders = claude.folder_servers()
    for folder, vault in cfg.bindings.items():
        wanted = set(folder_plan(cfg, folder, vault).servers) if vault in cfg.vaults else set()
        check(set(folders.get(folder, {})) == wanted, f"{contract(folder)} has the right servers", "vl apply")
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
    s.add_argument("--no-personal", action="store_true", help="don't create a personal vault")
    s.add_argument("--personal-remote", default=None, metavar="github|none|URL",
                   help="where the personal vault syncs (default: a private GitHub repo)")
    s.add_argument("--interval", type=int, help="seconds between background syncs (default 600)")
    s.set_defaults(func=cmd_init)

    vault = sub.add_parser("vault", help="create, join, adopt or remove vaults")
    vsub = vault.add_subparsers(dest="vault_command", required=True, metavar="ACTION")

    s = vsub.add_parser("create", help="create a new vault")
    s.add_argument("name")
    s.add_argument("--level", required=True, choices=LEVELS)
    s.add_argument("--team")
    s.add_argument("--remote", metavar="github|none|URL", help="default: a private GitHub repo")
    s.add_argument("--github", metavar="OWNER/REPO", help="GitHub repo to create (default OWNER/vault-NAME)")
    s.add_argument("--path", help="default: ~/Vaults/NAME")
    s.set_defaults(func=cmd_vault_create)

    s = vsub.add_parser("join", help="clone a vault someone else created")
    s.add_argument("url")
    s.add_argument("--level", required=True, choices=LEVELS)
    s.add_argument("--team")
    s.add_argument("--name", help="default: the repo name")
    s.add_argument("--path", help="default: ~/Vaults/NAME")
    s.set_defaults(func=cmd_vault_join)

    s = vsub.add_parser("adopt", help="manage a folder that is already a vault")
    s.add_argument("path")
    s.add_argument("--level", required=True, choices=LEVELS)
    s.add_argument("--team")
    s.add_argument("--name", help="default: the folder name")
    s.set_defaults(func=cmd_vault_adopt)

    s = vsub.add_parser("remove", help="stop managing a vault (keeps files unless --delete-files)")
    s.add_argument("name")
    s.add_argument("--delete-files", action="store_true")
    s.set_defaults(func=cmd_vault_remove)

    s = vsub.add_parser("list", help="same as `vl status`")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("bind", help="make a folder use a vault")
    s.add_argument("vault")
    s.add_argument("folder", nargs="?", help="default: the current folder")
    s.set_defaults(func=cmd_bind)

    s = sub.add_parser("unbind", help="make a folder use the default vault again")
    s.add_argument("folder", nargs="?", help="default: the current folder")
    s.set_defaults(func=cmd_unbind)

    s = sub.add_parser("follow", help="keep a git repo pulled on every sync (e.g. a team workspace)")
    s.add_argument("path")
    s.set_defaults(func=cmd_follow)

    s = sub.add_parser("unfollow", help="stop following a repo")
    s.add_argument("path")
    s.set_defaults(func=cmd_unfollow)

    s = sub.add_parser("sync", help="sync vaults now")
    s.add_argument("vault", nargs="?")
    s.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sync)

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
