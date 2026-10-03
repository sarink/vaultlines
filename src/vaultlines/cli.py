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
    rules,
    runtime,
    util,
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
        raise VlError(f"{text!r} isn't an owner on GitHub: a user or organization name.")
    return owner


# ---------------------------------------------------------------- vaults on disk

def _new_vault(vault_id: str, about: str, notes_from=(), source: dict | None = None,
               comments: dict | None = None) -> Path:
    """A new vault on this computer: a git repo with vault.toml."""
    path = vlt.path_of(vault_id)
    if path.exists() and any(path.iterdir()):
        raise VlError(f"{contract(path)} already exists.")
    path.mkdir(parents=True, exist_ok=True)
    (path / vlt.VAULT_FILE).write_text(vlt.render_vault_toml(about, notes_from, source, comments))
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
            if v.remote and v.owner in found and vid not in found[v.owner]["vaults"]}


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
        say(f"Made your personal vault {personal}. It's local until you publish it: "
            f"`vl vault publish {personal}`")
    return new


def _publish(cfg: Config, vault_id: str) -> None:
    """Put a local vault on GitHub, as a private repo."""
    v = cfg.vault(vault_id)
    if v.remote:
        raise VlError(f"{v.id} is already on GitHub ({v.remote}).")
    if github.exists(v.id):
        raise VlError(f"{v.id} already exists on GitHub, so this local vault can't be published under that name.")
    say(f"Creating the private GitHub repo {v.id}")
    github.create_private_repo(v.id, v.path)
    found = _discovery().get(v.owner, {}).get("vaults", [])
    _record(v.owner, [*found, v.id])
    a = github.audience(v.id)
    others = [x for x in a.logins if x.lower() != (cfg.me or _me())]
    if a.kind == "people" and others:
        warn(f"GitHub says these people can also see {v.id}: {', '.join(others)} "
             "(the owners of the organization, or its base permission).")
    else:
        say(f"Published {v.id}: a private repo that only you can access. Give people access on GitHub.")


# ---------------------------------------------------------------- init / join / leave

REQUIRED = {
    "git": "https://git-scm.com (on macOS: xcode-select --install)",
    "claude": "https://claude.com/claude-code",
    "gh": "https://cli.github.com (on macOS: brew install gh)",
}


def cmd_init(args) -> None:
    basic_memory = config.load_file().basic_memory
    required = dict(REQUIRED)
    if os.environ.get("VL_FAKE_GITHUB"):
        required.pop("gh")
    if basic_memory:
        required["uvx"] = "https://docs.astral.sh/uv/ (on macOS: brew install uv)"
    missing = [f"  {tool}: {hint}" for tool, hint in required.items() if not shutil.which(tool)]
    if missing:
        raise VlError("Install these first:\n" + "\n".join(missing))
    if not github.logged_in():
        say("Log in to GitHub. vl uses your account to find the vaults you can access.")
        subprocess.run(["gh", "auth", "login"])
        if not github.logged_in():
            raise VlError("Not logged in to GitHub. Run `gh auth login`, then `vl init` again.")
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
    _apply(config.load())
    say("Claude Code hooks installed: every session is guarded by vl.")
    cfg = config.load()
    if launchd.supported():
        launchd.install(cfg.sync_interval)
        say(f"Sync runs every {cfg.sync_interval // 60} min. Log: {contract(launchd.log_path())}")
    elif sys.platform != "darwin":
        say(f"No background sync on this system yet. Add a cron job: */10 * * * * {shutil.which('vl') or 'vl'} sync --background")
    say("\nDone. Next: `vl join OWNER` for an owner's vaults, like your organization's on GitHub. See `vl status`.")


def cmd_join(args) -> None:
    owner = _owner(args.owner)
    if not _initialized():
        say("Setting up vl on this computer first.")
        cmd_init(argparse.Namespace())
    me = _me()
    if owner != me and github.owner_kind(owner) is None:
        raise VlError(f"GitHub has no owner {owner} that you can see: no user or organization by that name.")
    fresh = not (vaults_dir() / owner).exists()
    try:
        _join(owner, me)
    except VlError:
        if fresh:
            shutil.rmtree(vaults_dir() / owner, ignore_errors=True)
        raise
    cfg = config.load()
    _apply(cfg)
    say(f"\nJoined {owner}. Its vaults on this computer:")
    _table([(vid, v.about or "-") for vid, v in sorted(cfg.vaults.items()) if v.owner == owner])
    for vid, v in sorted(cfg.vaults.items()):
        kind = plugins.SOURCES.get((v.source or {}).get("kind"))
        if v.owner == owner and kind and hasattr(kind, "saved_login") and not kind.validate_source(v.source) \
                and not kind.saved_login(v.source):
            say(f"\n{vid} holds notes from {kind.NAME}. To fetch originals, run `vl source login {vid}`.")
    say(f"\nRun `claude` in any {owner} repo: vl picks its vaults. See `vl status`.")


def _unique(path: Path) -> Path:
    n, found = 2, path
    while found.exists():
        found = path.with_name(f"{path.name}-{n}")
        n += 1
    return found


def cmd_leave(args) -> None:
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

def _kind(name: str):
    if name not in plugins.SOURCES:
        raise VlError(f"There's no source kind {name!r}. Kinds: {', '.join(sorted(plugins.SOURCES))}")
    return plugins.SOURCES[name]


def _new_vault_problem(cfg: Config, text: str) -> str | None:
    """Why `text` can't be the ID of a new vault, or None."""
    vault_id = text.strip().lower()
    if not vlt.valid_id(vault_id):
        return "Give OWNER/vault-NAME, like acme/vault-design."
    owner, repo = vault_id.split("/", 1)
    if not repo.startswith(vlt.PREFIX):
        return f"Vault repos start with {vlt.PREFIX}, like {owner}/{vlt.PREFIX}{repo}."
    if owner not in cfg.owners:
        return f"You haven't joined {owner}. Run `vl join {owner}` first."
    if vault_id in cfg.vaults or vlt.path_of(vault_id).exists():
        return f"{vault_id} is already on this computer."
    return None


def cmd_vault_create(args) -> None:
    notes_from = []
    for r in args.notes_from or []:
        if not vlt.REPO_ID_RE.match(r.strip()):
            raise VlError(f"--notes_from: {r!r} isn't a repo, written OWNER/REPO.")
        notes_from.append(r.strip().lower())
    cfg = config.load()
    if args.vault is None:
        if not util.interactive():
            raise VlError("Give the vault to create: OWNER/vault-NAME, like acme/vault-hq.")
        args.vault = util.ask("vault to create (OWNER/vault-NAME, like acme/vault-hq)",
                              check=lambda text: _new_vault_problem(cfg, text))
    problem = _new_vault_problem(cfg, args.vault)
    if problem:
        raise VlError(problem)
    vault_id = args.vault.strip().lower()
    if args.source:
        return _create_with_source(args, vault_id, notes_from)
    _new_vault(vault_id, args.about or "", notes_from)
    say(f"Made {vault_id}. It's local until you publish it: `vl vault publish {vault_id}`")
    _apply(config.load())


WORKFLOW_FILE = "vl-source.yml"
WORKFLOW = """\
# Written by vl. The refresh job: it refreshes this vault from its source (__NAME__).
name: vl source
on:
  schedule: [{ cron: "17 * * * *" }]        # hourly; edit to change
  workflow_dispatch: { inputs: { force: { type: boolean, default: false } } }
concurrency: { group: vl-source }            # one run at a time
permissions: { contents: write }
jobs:
  refresh:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
__STEPS__      - name: Fetch what changed, with the source's read-only login
        run: uvx --from "git+https://github.com/sarink/vaultlines@v__VERSION__" vl source refresh --fetch-only ${{ inputs.force && '--force' || '' }}
        env:
          VL_SOURCE_TOKEN: ${{ secrets.VL_SOURCE_TOKEN }}
      - name: Convert it, commit and push, without the login
        run: uvx --from "git+https://github.com/sarink/vaultlines@v__VERSION__" vl source refresh --convert-only
"""


def source_workflow(kind) -> str:
    steps = getattr(kind, "SETUP_STEPS", "").replace("__RCLONE__", getattr(kind, "RCLONE", ""))
    return WORKFLOW.replace("__NAME__", kind.NAME).replace("__STEPS__", steps).replace("__VERSION__", __version__)


def _create_with_source(args, vault_id: str, notes_from: list[str]) -> None:
    """A vault with a source. It's always published: its refresh job runs on GitHub."""
    kind = _kind(args.source)
    if notes_from:
        raise VlError("a vault with a source can't take notes, so it has no notes_from.")
    source = {"kind": args.source, **getattr(kind, "DEFAULTS", {})}
    source.update({key: getattr(args, key) for key in kind.OPTIONS if getattr(args, key, None) is not None})
    missing = [key for key in getattr(kind, "REQUIRED", ()) if not str(source.get(key) or "").strip()]
    problems = [p for p in kind.validate_source(source) if p.split(":", 1)[0] not in missing]
    if problems:
        raise VlError("; ".join(problems))
    guide = getattr(kind, "GUIDE", "")
    if missing and not util.interactive():
        raise VlError(f"Missing {', '.join('--' + key for key in missing)}." + (f"\n\n{guide}" if guide else ""))
    if github.exists(vault_id):
        raise VlError(f"{vault_id} already exists on GitHub.")
    if "workflow" not in github.scopes():
        raise VlError("Your GitHub login can't add workflow files, and the vault's refresh job needs one. "
                      "Run `gh auth refresh -h github.com -s workflow`, then try again.")
    if missing:
        say(guide)
        for key in missing:
            if key not in getattr(kind, "LATER", ()):
                source[key] = util.ask(f"{key} ({kind.OPTIONS[key]})")
    source, secret, name = kind.create(vault_id, source, util.ask)
    source = {key: source[key] for key in ("kind", *kind.OPTIONS) if key in source}  # in the kind's order
    problems = kind.validate_source(source)
    if problems:
        raise VlError("; ".join(problems))
    about = args.about or (kind.default_about(name) if hasattr(kind, "default_about") else "")
    comments = kind.comments(source, name) if hasattr(kind, "comments") else None
    path = vlt.path_of(vault_id)
    try:
        path.mkdir(parents=True)
        (path / ".github" / "workflows").mkdir(parents=True)
        (path / ".github" / "workflows" / WORKFLOW_FILE).write_text(source_workflow(kind))
        (path / vlt.VAULT_FILE).write_text(vlt.render_vault_toml(about, (), source, comments))
        gitsync.init_repo(path)
        say(f"Creating the private GitHub repo {vault_id}")
        github.create_private_repo(vault_id, path)
    except VlError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    github.set_secret(vault_id, "VL_SOURCE_TOKEN", secret)
    github.run_workflow(vault_id, WORKFLOW_FILE)
    owner = vault_id.split("/")[0]
    _record(owner, [*_discovery().get(owner, {}).get("vaults", []), vault_id])
    _apply(config.load())
    say(f"\nMade {vault_id}. The vault is refreshed every hour by a GitHub Action that reads {kind.NAME}.")
    say(f"To refresh manually, run: `vl source refresh {vault_id} [--force]`.")
    say(_who_can_access(vault_id))


def _who_can_access(vault_id: str) -> str:
    try:
        a = github.audience(vault_id)
    except (VlError, github.Unreachable) as e:
        return f"vl couldn't ask GitHub who can access this vault ({e})."
    if a.kind == "people":
        return f"These users can access this vault: {', '.join(a.logins) or 'nobody'}."
    if a.kind == "everyone":
        return "Everyone can access this vault: it's a public repo."
    return f"vl couldn't tell who can access this vault: {a.reason}."


def cmd_vault_publish(args) -> None:
    _publish(config.load(), args.vault)
    _apply(config.load())


# ---------------------------------------------------------------- source refresh / fetch / login

def _source_vault(ref: str):
    """(vault, its kind, its [source] table) for a vault with a source."""
    v = config.load().vault(ref)
    if v.source is None:
        raise VlError(f"{v.id} has no source.")
    kind = _kind(v.source.get("kind"))
    problems = kind.validate_source(v.source)
    if problems:
        raise VlError(f"{v.id}'s vault.toml has problems in [source]: " + "; ".join(problems))
    return v, kind, v.source


def cmd_source_refresh(args) -> None:
    """Fetch what changed (the only half with a login), then convert it, commit and push."""
    if args.convert_only and args.force:
        raise VlError("--force goes with the fetch: the convert does what the fetch planned.")
    root, vault_id, kind, source = _refresh_target(args.vault)
    staged = util.refresh_dir(vault_id)
    again = "vl source refresh" + (f" {args.vault}" if args.vault else "")
    if not args.convert_only:
        say(f"Refreshing {vault_id} from {kind.NAME}...")
        _take_remote(root)
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True)
        fetched = kind.fetch_changes(root, source, vault_id, lambda: _source_login(kind, source, vault_id),
                                     args.force, staged)
        say(fetched + (f". Next: `{again} --convert-only`." if args.fetch_only else ""))
        if args.fetch_only:
            return
    elif not staged.is_dir():
        raise VlError(f"Nothing fetched for {vault_id}. Run `{again} --fetch-only` first.")
    try:
        _take_remote(root)
        _convert(root, kind, source, vault_id, staged)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _refresh_target(ref: str | None):
    """(clone, vault ID, kind, [source]) of the vault to refresh: the one named, or else the
    one this folder is in."""
    if ref:
        v, kind, source = _source_vault(ref)
        return v.path, v.id, kind, source
    found = rules.repo_of(os.getcwd())
    if not found or not found[1] or not (Path(found[0]) / vlt.VAULT_FILE).exists():
        raise VlError("Give the vault to refresh, like `vl source refresh acme/vault-hq`, or run it in the vault's "
                      "clone.")
    root, vault_id = Path(found[0]), found[1][0]
    info, _ = vlt.parse_vault_toml((root / vlt.VAULT_FILE).read_text())
    if info.source is None:
        raise VlError(f"{vault_id} has no source.")
    kind = _kind(info.source.get("kind"))
    problems = kind.validate_source(info.source)
    if problems:
        raise VlError(f"{vault_id}'s vault.toml has problems in [source]: " + "; ".join(problems))
    return root, vault_id, kind, info.source


def _source_login(kind, source: dict, vault_id: str) -> str:
    """The login a refresh uses: VL_SOURCE_TOKEN if it's set, or else your own on this computer."""
    token = os.environ.get("VL_SOURCE_TOKEN", "").strip()
    if token:
        return token
    saved = kind.saved_login(source) if hasattr(kind, "saved_login") else None
    if not saved and util.interactive() and hasattr(kind, "login"):
        say(f"Log in to {kind.NAME}, read-only. A browser opens.")
        kind.login(source)
        saved = kind.saved_login(source)
    if not saved:
        raise VlError(f"No login for {kind.NAME}: set VL_SOURCE_TOKEN, or run `vl source login {vault_id}`.")
    return saved


def _take_remote(root: Path) -> None:
    """Start from the vault as it is on GitHub."""
    if gitsync.remote_url(root):
        status = gitsync.pull_keeping_changes(root)
        if status.startswith("synced."):
            say(status[len("synced. "):])


def _convert(root: Path, kind, source: dict, vault_id: str, staged: Path) -> None:
    if not gitsync.git(root, "config", "user.email", check=False).stdout.strip():
        gitsync.git(root, "config", "user.name", "github-actions[bot]")
        gitsync.git(root, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    try:
        status = kind.convert(root, source, vault_id, staged)
    except VlError:
        if gitsync.commit(root, f"Partial update from {kind.NAME}"):  # what did arrive is kept
            _push(root)
        raise
    if gitsync.commit(root, f"Update from {kind.NAME}"):
        _push(root)
        say(status)
    else:
        say(f"no changes ({status})")


def _push(root: Path) -> None:
    """Push, and if another refresh pushed first, put these notes on top of its and try again."""
    if not gitsync.remote_url(root):
        return
    for _ in range(3):
        if gitsync.git(root, "push", "-q", "origin", "HEAD", check=False).returncode == 0:
            return
        if gitsync.git(root, "pull", "-q", "--rebase", "origin", gitsync.branch(root), check=False).returncode != 0:
            gitsync.git(root, "rebase", "--abort", check=False)
            break
    raise VlError("couldn't push the notes")


def cmd_source_fetch(args) -> None:
    v, kind, source = _source_vault(args.vault)
    if not hasattr(kind, "fetch"):
        raise VlError(f"{kind.NAME} sources have no originals to fetch.")
    say(str(kind.fetch(v, source, args.path)))


def cmd_source_login(args) -> None:
    _, kind, source = _source_vault(args.vault)
    if not hasattr(kind, "login"):
        raise VlError(f"{kind.NAME} sources have no login.")
    kind.login(source)
    say(f"Logged in to {kind.NAME} with your own account, read-only. `vl source fetch` and `vl source refresh` "
        "use it on this computer.")


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


def _apply(cfg: Config, fresh: bool = True) -> Applied:
    """Make git, the plugins, Claude Code and Obsidian match the vaults and config.toml."""
    warnings: list[str] = []
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
    launchd.keep_current(cfg.sync_interval)

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
        return gitsync.pull_keeping_changes(v.path)  # its refresh job writes it; here it's only read
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
        daily = args.check_github or (
            not only and time.time() - audience.load_state().get("checked_at", 0) >= cfg.check_interval)
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
        notify(f"vl can't guard some places: {len(new)} settings file(s) set disableAllHooks. Run `vl sync --check-github`.")


# ---------------------------------------------------------------- check / status / sessions / doctor

def preview(writes: str, reads: list[str], auds: dict[str, Audience], me: str) -> list[str]:
    """Where writes will ask, given who can see each vault."""
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
    kind = plugins.SOURCES.get((v.source or {}).get("kind"))
    if v.source is not None:
        name = kind.NAME if kind else v.source.get("kind")
        when = gitsync.git(v.path, "log", "-1", "--format=%cr", f"--grep=Update from {name}", check=False).stdout.strip()
        return f"from {name}, refreshed {when or 'never'}"
    pending = gitsync.pending_changes(v.path)
    where = "published" if v.remote else "local"
    return f"{where}, last commit {gitsync.last_commit_age(v.path)}" + (f", {pending} unsaved" if pending else "")


def cmd_status(args) -> None:
    cfg = config.load()
    if not cfg.vaults:
        say("No vaults yet. Run `vl init`.")
        return
    me = cfg.me
    auds, _ = audience.audiences(cfg.vaults, fresh=False)
    gone = lost(cfg)
    data = runtime.load() or {}
    owners = sorted({v.owner for v in cfg.vaults.values()} | set(cfg.owners), key=lambda o: (o != me, o))
    for owner in owners:
        say(_owner_title(owner, me))
        rows = [("vault", "state", "who can see it")]
        for vid, v in sorted(cfg.vaults.items()):
            if v.owner == owner:
                rows.append((vid, _vault_state(v, gone), auds[vid].describe()))
        if len(rows) > 1:
            _table(rows)
        o = (data.get("owners") or {}).get(owner)
        if o:
            for repo, vid in o["notes_from"].items():
                say(f"  notes from {repo} -> {vid}")
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
    asks = [line for o in (data.get("owners") or {}).values()
            for w in sorted({o["personal"], *o["notes_from"].values()} - {None})
            for line in preview(w, [v for v in o["vaults"] if v != w], auds, me)]
    say("Where writes will ask")
    for line in asks or ["never"]:
        say(f"  {line}")
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
    say(f"Last checked with GitHub: {time.strftime('%Y-%m-%d %H:%M', time.localtime(checked)) if checked else 'never'}"
        " (`vl sync --check-github` checks now)")
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
    sourced = [(vid, v) for vid, v in sorted(cfg.vaults.items()) if v.source is not None]
    if sourced:
        say("Sources")
    for vid, v in sourced:
        kind = plugins.SOURCES.get(v.source.get("kind"))
        if kind is None:
            check(False, f"{vid}: source kind {v.source.get('kind')!r}", f"vl knows: {', '.join(plugins.SOURCES)}")
            continue
        problems = kind.validate_source(v.source)
        check(not problems, f"{vid}: [source] in vault.toml is complete", "; ".join(problems))
        if not problems and hasattr(kind, "saved_login"):
            on = kind.saved_login(v.source) is not None
            what = "for fetching originals and refreshing on this computer"
            say(f"  {'ok  ' if on else 'note'}  {vid}: " + (f"logged in to {kind.NAME} with your own account, {what}"
                                                          if on else f"not logged in with your own account, {what} "
                                                          f"(`vl source login {vid}`)"))
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
        check(not unchecked, "every vault's audience is known to the hook", "vl sync --check-github")
    if launchd.supported():
        say("Sync")
        check(launchd.loaded(), "background sync is on", "vl init")
    say("\nAll good." if ok else "\nSome checks failed.")
    if not ok:
        sys.exit(1)


def cmd_hook(args) -> None:
    from .hook import main as hook_main

    hook_main()


# ---------------------------------------------------------------- main

def _wanted_kind(argv: list[str]) -> str | None:
    """The KIND in `vl vault create ... --source KIND`, so only that kind's keys become flags."""
    if argv[:2] != ["vault", "create"]:
        return None
    for i, arg in enumerate(argv):
        if arg == "--source" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--source="):
            return arg.split("=", 1)[1]
    return None


def build_parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vl", description="Notes for Claude Code, shared through GitHub.")
    p.add_argument("--version", action="version", version=f"vaultlines {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("init", help="set up this computer: GitHub login, your personal vault, hooks").set_defaults(
        func=cmd_init)
    s = sub.add_parser("join", help="get the vaults of an owner on GitHub (a user or organization) that you can "
                       "access")
    s.add_argument("owner", metavar="OWNER")
    s.set_defaults(func=cmd_join)
    s = sub.add_parser("leave", help="stop using an owner's vaults (keeps the files unless --delete-files)")
    s.add_argument("owner", metavar="OWNER")
    s.add_argument("--delete-files", action="store_true", help="delete them, instead of moving them to "
                   "~/.vaultlines/left/")
    s.set_defaults(func=cmd_leave)

    vault = sub.add_parser("vault", help="create or publish a vault")
    vsub = vault.add_subparsers(dest="vault_command", required=True, metavar="ACTION")
    kinds = ", ".join(sorted(plugins.SOURCES))
    kind = plugins.SOURCES.get(_wanted_kind(argv or []))
    epilog = f"Source kinds: {kinds}. For a kind's keys: vl vault create --source KIND --help"
    if kind is not None:
        epilog = f"Keys you leave out are asked for.\n\n{getattr(kind, 'GUIDE', '')}".strip()
    s = vsub.add_parser("create", help="a new vault, local until you publish it", epilog=epilog,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("vault", metavar="VAULT", nargs="?", help="OWNER/vault-NAME")
    s.add_argument("--about", metavar="TEXT", help="one line about the vault, for Claude")
    s.add_argument("--notes_from", metavar="REPO", action="append", help="a repo whose notes go here (OWNER/REPO); "
                   "give it again for more")
    s.add_argument("--source", metavar="KIND", help=f"the vault's notes come from a source ({kinds}). The vault "
                   "is published, and its refresh job runs on GitHub")
    if kind is not None:
        group = s.add_argument_group(f"{_wanted_kind(argv)} source: each key goes into [source] in vault.toml")
        for key, text in kind.OPTIONS.items():
            group.add_argument(f"--{key}", metavar="VALUE", help=text)
    s.set_defaults(func=cmd_vault_create)
    s = vsub.add_parser("publish", help="publish a vault: a private repo on GitHub")
    s.add_argument("vault", metavar="VAULT")
    s.set_defaults(func=cmd_vault_publish)

    source = sub.add_parser("source", help="vaults with a source")
    ssub = source.add_subparsers(dest="source_command", required=True, metavar="ACTION")
    s = ssub.add_parser("refresh", help="refresh a vault from its source, on this computer",
                        description="Refresh a vault from its source, on this computer: fetch what changed, "
                        "convert it, commit and push. It logs in with VL_SOURCE_TOKEN if that's set, or else "
                        "with your own login (`vl source login`).")
    s.add_argument("vault", metavar="VAULT", nargs="?", help="OWNER/vault-NAME (default: the vault this folder is in)")
    s.add_argument("--force", action="store_true", help="rebuild every note from scratch")
    only = s.add_mutually_exclusive_group()
    only.add_argument("--fetch-only", action="store_true", help="only fetch what changed, with the login")
    only.add_argument("--convert-only", action="store_true", help="only convert what --fetch-only fetched, "
                      "commit and push, without the login")
    s.set_defaults(func=cmd_source_refresh)
    s = ssub.add_parser("fetch", help="fetch one original into the fetch folder and print where it is")
    s.add_argument("vault", metavar="VAULT")
    s.add_argument("path", metavar="PATH", help="the `path` from the note's frontmatter")
    s.set_defaults(func=cmd_source_fetch)
    s = ssub.add_parser("login", help="log in to the vault's source with your own account, for fetching originals "
                        "and refreshing on this computer")
    s.add_argument("vault", metavar="VAULT")
    s.set_defaults(func=cmd_source_login)

    s = sub.add_parser("sync", help="sync vaults now (and once a day, check with GitHub)")
    s.add_argument("vault", metavar="VAULT", nargs="?", help="only this vault")
    s.add_argument("--check-github", action="store_true", help="check with GitHub now: new vaults, lost ones, "
                   "who can see each vault")
    s.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("sessions", help="recent Claude sessions and what they read (for debugging)")
    s.add_argument("--limit", metavar="N", type=int, default=20, help="show the N most recent (default: 20)")
    s.set_defaults(func=cmd_sessions)

    sub.add_parser("status", help="vaults by owner, who can see each one, where writes will ask").set_defaults(func=cmd_status)
    sub.add_parser("apply", help="set up git, Basic Memory and Claude Code again").set_defaults(func=cmd_apply)
    sub.add_parser("doctor", help="check that everything is set up").set_defaults(func=cmd_doctor)
    sub.add_parser("uninstall", help="remove the hooks and stop background sync").set_defaults(func=cmd_uninstall)
    sub.add_parser("hook", help=argparse.SUPPRESS).set_defaults(func=cmd_hook)
    return p


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser(argv).parse_args(argv)
    try:
        args.func(args)
    except VlError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
