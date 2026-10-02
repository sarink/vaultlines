"""`vl migrate`: move a vl 0.3 setup into ~/.vaultlines, once.

1. vl 0.3's files (~/.config, ~/.local/state and ~/.cache /vaultlines) are read; sessions
   and cached answers from GitHub are copied. The old folders stay, with a MOVED.txt.
2. A vault with a GitHub remote moves to vaults/OWNER/REPO if its repo follows the new
   rules (a `vault-` name and vault.toml, after any rename on GitHub). Otherwise it needs
   an admin step first, and stays where it is.
3. A vault on this computer only: `personal` becomes your personal vault, others become
   local/NAME, unless --map says otherwise (a new private repo on GitHub, for an org vault).
4. [folders] entries: a folder that is a clone of a repo of an owner you join becomes a
   [repos."OWNER/REPO"] entry, so it works wherever the repo is cloned; other folders stay
   [folders] entries. Nothing changes until notes_from takes over.
5. Basic Memory, Obsidian and launchd follow the new places, then `vl apply`.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import audience, config, github, gitsync, launchd, obsidian, rules
from . import vaults as vlt
from .util import VlError, clones_path, contract, expand, home, say, state_dir, vaults_dir, write_json

MOVED = """vaultlines 0.4 moved what was here to ~/.vaultlines on {date}:
  config.toml        -> ~/.vaultlines/config.toml (only your own changes now)
  state and sessions -> ~/.vaultlines/state/
  vaults             -> ~/.vaultlines/vaults/OWNER/REPO
Nothing here is used any more. You can delete this folder.
"""


@dataclass
class OldVault:
    name: str
    path: Path
    remote: str | None


@dataclass
class Old:
    config_dir: Path
    state_dir: Path
    cache_dir: Path
    vaults_dir: Path
    settings: dict
    vaults: dict[str, OldVault]
    folders: dict[str, dict]
    basic_memory: bool
    state: dict


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or str(home() / default)) / "vaultlines"


def find_old() -> Old | None:
    config_dir = _xdg("XDG_CONFIG_HOME", ".config")
    path = config_dir / "config.toml"
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise VlError(f"{contract(path)}: {e}") from None
    settings = data.get("settings", {})
    found = {name: OldVault(name, expand(v["path"]), v.get("remote"))
             for name, v in data.get("vaults", {}).items() if isinstance(v, dict) and "path" in v}
    folders = {str(expand(p)): f for p, f in data.get("folders", {}).items() if isinstance(f, dict)}
    plugins = data.get("plugins", {})
    try:
        state = json.loads((config_dir / "state.json").read_text())
    except (OSError, ValueError):
        state = {}
    return Old(config_dir, _xdg("XDG_STATE_HOME", ".local/state"), _xdg("XDG_CACHE_HOME", ".cache"),
               expand(settings.get("vaults_dir", "~/Vaults")), settings, found, folders,
               any(isinstance(p, dict) and p.get("kind") == "basic-memory" for p in plugins.values()), state)


# ---------------------------------------------------------------- the plan

@dataclass
class Move:
    old: OldVault
    target: str | None  # the vault ID it becomes; None: it stays (see `why`)
    how: str  # "move" (a clone that follows the rules), "local", "create" (a new repo on GitHub), "stay"
    why: str = ""


@dataclass
class Plan:
    me: str
    moves: list[Move] = field(default_factory=list)
    repos: dict[str, dict] = field(default_factory=dict)  # OWNER/REPO -> entry
    folders: dict[str, dict] = field(default_factory=dict)  # folder -> entry
    clones: dict[str, str] = field(default_factory=dict)  # repo top folder -> OWNER/REPO
    skipped: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)

    @property
    def ids(self) -> dict[str, str]:
        return {m.old.name: m.target for m in self.moves if m.target}


def parse_maps(texts: list[str], old: Old) -> dict[str, str]:
    out = {}
    for text in texts or []:
        name, sep, target = text.partition("=")
        name, target = name.strip(), target.strip().lower()
        if not sep or name not in old.vaults:
            raise VlError(f"--map takes OLD_NAME=OWNER/REPO, with a vault from your 0.3 config "
                          f"({', '.join(sorted(old.vaults))}), not {text!r}")
        if not vlt.valid_id(target):
            raise VlError(f"--map {text}: {target!r} isn't OWNER/REPO")
        owner, repo = target.split("/", 1)
        if owner == vlt.LOCAL:
            if not config.LOCAL_RE.match(target):
                raise VlError(f"--map {text}: local vault names use lowercase letters, digits and dashes")
        elif not repo.startswith(vlt.PREFIX):
            raise VlError(f"--map {text}: vault repos start with {vlt.PREFIX}, like {owner}/{vlt.PREFIX}{repo}")
        out[name] = target
    return out


def _admin_step(rid: str | None, remote: str) -> str:
    if not rid:
        return f"needs an admin step: its remote {remote} isn't on GitHub"
    owner, repo = rid.split("/", 1)
    new = repo if repo.startswith(vlt.PREFIX) else f"{vlt.PREFIX}{repo}"
    rename = "" if repo.startswith(vlt.PREFIX) else f"rename {rid} to a name that starts with {vlt.PREFIX} (like {owner}/{new}), "
    return f"needs an admin step: on GitHub, {rename}add vault.toml (`about = \"...\"`), then run `vl migrate` again"


def make_plan(old: Old, me: str, maps: dict[str, str]) -> Plan:
    plan = Plan(me)
    listed: dict[str, dict[str, str]] = {}

    def vault_repos(owner: str) -> dict[str, str]:
        if owner not in listed:
            try:
                listed[owner] = github.vault_repos(owner)
            except github.Unreachable as e:
                raise VlError(f"Couldn't ask GitHub for {owner}'s vaults: {e}") from None
        return listed[owner]

    for name, v in sorted(old.vaults.items()):
        if not v.path.is_dir():
            plan.moves.append(Move(v, None, "stay", f"its folder {contract(v.path)} is missing"))
            continue
        target = maps.get(name)
        if v.remote:
            rid = vlt.remote_id(v.remote)
            now = github.canonical(rid) if rid else None
            if now and (target in (None, now)) and now in vault_repos(now.split("/")[0]):
                plan.moves.append(Move(v, now, "move"))
            else:
                plan.moves.append(Move(v, None, "stay", _admin_step(now or rid, v.remote)))
        elif target and not target.startswith(vlt.LOCAL + "/") and target != vlt.personal_id(target.split("/")[0], me):
            if github.exists(target):
                plan.moves.append(Move(v, None, "stay", f"{target} already exists on GitHub; merge the notes by hand"))
            else:
                plan.moves.append(Move(v, target, "create"))
        else:
            plan.moves.append(Move(v, target or (vlt.personal_id(me, me) if name == "personal" else f"local/{name}"),
                                   "local"))
    targets = [m.target for m in plan.moves if m.target]
    for t in set(targets):
        if targets.count(t) > 1:
            raise VlError(f"Two vaults would become {t}. Use --map to pick another name for one.")
        if vlt.path_of(t).exists():
            raise VlError(f"{contract(vlt.path_of(t))} already exists.")
    plan.owners = sorted({me} | {t.split("/")[0] for t in targets if not t.startswith(vlt.LOCAL + "/")})

    ids = plan.ids
    default = vlt.personal_id(me, me)
    for folder, f in sorted(old.folders.items()):
        writes, reads = ids.get(f.get("writes")), [ids.get(r) for r in f.get("reads", [])]
        if writes is None or None in reads:
            missing = [n for n in [f.get("writes"), *f.get("reads", [])] if not ids.get(n)]
            plan.skipped.append(f"{contract(folder)}: uses {', '.join(missing)}, which wasn't moved")
            continue
        if not Path(folder).is_dir():
            plan.skipped.append(f"{contract(folder)}: the folder is gone")
            continue
        found = rules.repo_of(folder)
        repo = None
        if found and found[0] == os.path.realpath(folder):
            repo = next((r for r in found[1] if r.split("/")[0] in plan.owners), None)
        entry = {"writes": writes, "reads": reads}
        if repo:
            plan.repos[repo] = {**entry, "auto_pull": bool(f.get("auto_pull"))}
            plan.clones[found[0]] = repo
        elif writes == default and not reads and Path(folder) == home():
            continue  # "~" with your personal vault is what vl does anyway
        else:
            if f.get("auto_pull"):
                plan.skipped.append(f"{contract(folder)}: auto_pull now needs a repo of an owner you joined")
            plan.folders[folder] = entry
    return plan


def show(plan: Plan) -> None:
    say("Vaults")
    for m in plan.moves:
        if m.how == "stay":
            say(f"  {m.old.name}: {m.why}")
        else:
            extra = " (a new private repo on GitHub)" if m.how == "create" else ""
            say(f"  {m.old.name} -> {m.target}{extra}   from {contract(m.old.path)}")
    if plan.repos or plan.folders:
        say("\nconfig.toml")
        for key, e in plan.repos.items():
            say(f"  [repos.\"{key}\"] writes {e['writes']}" + (f", reads {', '.join(e['reads'])}" if e["reads"] else "")
                + (", auto_pull" if e["auto_pull"] else ""))
        for key, e in plan.folders.items():
            say(f"  [folders.\"{contract(key)}\"] writes {e['writes']}"
                + (f", reads {', '.join(e['reads'])}" if e["reads"] else ""))
    for line in plan.skipped:
        say(f"  left out: {line}")
    say(f"\nOwners joined: {', '.join(plan.owners)}")


# ---------------------------------------------------------------- doing it

def _toml(value) -> str:
    return json.dumps(value)


def _entries(plan: Plan, cfg: config.Config) -> str:
    out = []
    for vid in sorted({m.target for m in plan.moves if m.target and m.target.startswith(vlt.LOCAL + "/")}):
        if vid not in cfg.local:
            out.append(f'[vaults."{vid}"]')
    for key, e in plan.repos.items():
        if key in cfg.repos:
            continue
        lines = [f'[repos."{key}"]', f"writes    = {_toml(e['writes'])}"]
        if e["reads"]:
            lines.append(f"reads     = {_toml(e['reads'])}")
        if e["auto_pull"]:
            lines.append("auto_pull = true")
        out.append("\n".join(lines))
    for key, e in plan.folders.items():
        if key in cfg.folders:
            continue
        lines = [f'[folders."{contract(key)}"]', f"writes = {_toml(e['writes'])}"]
        if e["reads"]:
            lines.append(f"reads  = {_toml(e['reads'])}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _write_config(old: Old, plan: Plan) -> None:
    config.write_template(basic_memory=old.basic_memory)
    for key in ("sync_interval", "check_interval", "on_leak"):
        if key in old.settings and old.settings[key] != config.SETTINGS[key]:
            config.set_setting(key, _toml(old.settings[key]))
    entries = _entries(plan, config.load_file())
    if entries:
        config.append("# ---------------------------------------------------------------- from vl 0.3 (vl migrate)\n"
                      + entries)


def _ensure_vault_toml(path: Path, about: str) -> None:
    if not (path / vlt.VAULT_FILE).exists():
        (path / vlt.VAULT_FILE).write_text(vlt.render_vault_toml(about))
        gitsync.ensure_identity(path)
        gitsync.commit(path, "Add vault.toml")


def _move(m: Move, me: str) -> None:
    dest = vlt.path_of(m.target)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(m.old.path), str(dest))
    if m.how == "move":
        if vlt.remote_id(gitsync.remote_url(dest)) != m.target:
            gitsync.set_remote(dest, github.clone_url(m.target))
        return
    personal = m.target == vlt.personal_id(m.target.split("/")[0], me)
    _ensure_vault_toml(dest, f"{me}'s personal notes." if personal and m.target.startswith(me + "/")
                       else f"{me}'s personal notes in {m.target.split('/')[0]}." if personal else "")
    if m.how == "create":
        say(f"Creating the private GitHub repo {m.target}")
        github.create_private_repo(m.target, dest)


def _copy_state(old: Old) -> None:
    if (old.state_dir / "sessions").is_dir():
        shutil.copytree(old.state_dir / "sessions", state_dir() / "sessions", dirs_exist_ok=True)
    state = audience.load_state()
    for key in ("audiences", "me"):
        if key in old.state and key not in state:
            state[key] = old.state[key]
    audience.save_state(state)


def _old_blocks(old: Old) -> None:
    """Remove the Basic Memory blocks vl 0.3 wrote in listed folders: they name old projects."""
    from . import claude

    blocks = ((old.state.get("plugins") or {}).get("basic-memory") or {}).get("state", {}).get("blocks", [])
    for path in blocks:
        if Path(path) != claude.plugin_user_settings_path() and Path(path).exists():
            claude.update_settings(Path(path), None)


def _old_projects(old: Old, moved: list[Move]) -> None:
    """Basic Memory knows the vaults by their old names and folders. `vl apply` registers the new ones."""
    from .plugins import basic_memory as bm

    if not old.basic_memory:
        return
    settings = {"kind": "basic-memory"}
    try:
        current = bm.projects(settings)
    except VlError:
        return
    for m in moved:
        if m.old.name in current:
            bm.remove_project(settings, m.old.name)


def _moved_note(folders: list[Path]) -> None:
    text = MOVED.format(date=time.strftime("%Y-%m-%d"))
    for folder in folders:
        if folder.is_dir():
            (folder / "MOVED.txt").write_text(text)


def migrate(args) -> None:
    from .cli import _apply, _join

    if vlt.on_disk():
        raise VlError(f"This computer is already set up in {contract(vaults_dir())}, so there's nothing to migrate.")
    old = find_old()
    if old is None:
        raise VlError("No vl 0.3 setup found (~/.config/vaultlines/config.toml). Run `vl init` instead.")
    maps = parse_maps(args.map, old)
    me = github.login() or old.state.get("me", "")
    if not me:
        raise VlError("Couldn't get your GitHub login. Run `gh auth login` first.")
    plan = make_plan(old, me, maps)
    show(plan)
    if args.dry_run:
        say("\nThis was a dry run: nothing changed. Run without --dry-run to do it.")
        return

    state = audience.load_state()
    state["me"] = me
    audience.save_state(state)
    _copy_state(old)
    moved = [m for m in plan.moves if m.target]
    _old_projects(old, moved)
    paths = {}
    for m in moved:
        paths[str(m.old.path)] = str(vlt.path_of(m.target))
        _move(m, me)
    _write_config(old, plan)
    if plan.clones:
        found = {}
        try:
            found = json.loads(clones_path().read_text())
        except (OSError, ValueError):
            pass
        write_json(clones_path(), {**found, **plan.clones})
    _old_blocks(old)
    for owner in plan.owners:
        _join(owner, me)
    obsidian.relocate(paths)
    if launchd.supported():
        launchd.install(config.load_file().sync_interval)
    _moved_note([old.config_dir, old.state_dir, old.cache_dir, old.vaults_dir])
    _apply(config.load())

    say("\nDone. Your vaults are in ~/.vaultlines/vaults. See `vl status`.")
    waiting = [m for m in plan.moves if m.how == "stay"]
    if waiting:
        say("Still to do:")
        for m in waiting:
            say(f"  {m.old.name}: {m.why}")
    suggest = [(k, e) for k, e in plan.repos.items() if e["writes"].split("/")[0] == k.split("/")[0]]
    if suggest:
        say("These config.toml entries can go once the vault lists the repo in notes_from (in its vault.toml):")
        for key, e in suggest:
            keep = " (keep the entry for auto_pull)" if e["auto_pull"] else ""
            say(f"  [repos.\"{key}\"]: add \"{key}\" to notes_from in {e['writes']}{keep}")
