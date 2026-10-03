"""~/.vaultlines/config.toml: your own changes. vl works without any of them.

vl writes the file once, with only comments and examples, and never rewrites it. What
vl works with (`Config`) is the vaults on disk plus this file.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import vaults as vlt
from .util import VlError, contract, expand, vl_home

SETTINGS = {"sync_interval": 600, "check_interval": 86400, "on_leak": "ask", "basic_memory": True}
RULE_KEYS = {"writes", "reads", "auto_pull", "dangerously_skip_hook_guards"}
FOLDER_KEYS = {"writes", "reads"}
ON_LEAK = ("ask", "block")
REPO_KEY_RE = re.compile(r"^[A-Za-z0-9-]+/(?:\*|[A-Za-z0-9._-]+)$")

TEMPLATE = """\
# ~/.vaultlines/config.toml
# Your own changes. vl works without any of them. Run `vl apply` after editing.

# ---------------------------------------------------------------- settings
# sync_interval  = 600      # seconds between background syncs
# check_interval = 86400    # seconds between checks with GitHub
# on_leak        = "ask"    # or "block"
# basic_memory   = true     # false: don't set up Basic Memory

# ---------------------------------------------------------------- repos
# Change the rules for one repo, wherever it is cloned.
# [repos."acme/website"]
# writes    = "acme/vault-public"
# reads     = ["kabir/vault-recipes"]      # added to the owner's vaults
# auto_pull = true                         # `git pull --ff-only` it on every sync
#
# Dev mode, for working on vl itself: in sessions in this repo, vl's hook guards nothing.
# Claude can read and change every vault and all of vl's own files (your Google logins
# too), and nothing asks first.
# [repos."sarink/vaultlines"]
# dangerously_skip_hook_guards = true
#
# Let Claude read another owner's vault in every acme repo (owners are kept apart by default):
# [repos."acme/*"]
# reads = ["kabir/vault-side"]

# ---------------------------------------------------------------- folders
# For folders that aren't in a repo of an owner you joined.
# [folders."~/Documents/writing"]
# writes = "kabir/vault-recipes"
"""
BASIC_MEMORY_LINE = "# basic_memory   = true     # false: don't set up Basic Memory"


def config_path() -> Path:
    return vl_home() / "config.toml"


@dataclass
class Rule:
    """A [repos."OWNER/REPO"] or [folders."path"] entry. Vaults are IDs, like OWNER/REPO."""
    key: str  # OWNER/REPO, OWNER/*, or an absolute folder
    writes: str | None = None
    reads: list[str] = field(default_factory=list)
    auto_pull: bool = False
    dangerously_skip_hook_guards: bool = False  # dev mode: the hook guards nothing in sessions here


@dataclass
class Config:
    sync_interval: int = SETTINGS["sync_interval"]
    check_interval: int = SETTINGS["check_interval"]
    on_leak: str = SETTINGS["on_leak"]
    basic_memory: bool = SETTINGS["basic_memory"]
    repos: dict[str, Rule] = field(default_factory=dict)  # OWNER/REPO or OWNER/* -> rule
    folders: dict[str, Rule] = field(default_factory=dict)  # absolute folder -> rule
    # What's on disk (filled by load()):
    vaults: dict[str, vlt.Vault] = field(default_factory=dict)  # ID -> vault
    owners: list[str] = field(default_factory=list)  # joined
    me: str = ""

    @property
    def shorts(self) -> dict[str, str]:
        """Vault ID -> short name."""
        return vlt.short_names(self.vaults)

    def vault(self, ref: str) -> vlt.Vault:
        """A vault by ID (OWNER/REPO) or short name."""
        ref = ref.strip().lower()
        if ref in self.vaults:
            return self.vaults[ref]
        by_short = {s: i for i, s in self.shorts.items()}
        if ref in by_short:
            return self.vaults[by_short[ref]]
        known = ", ".join(sorted(self.vaults)) or "none"
        raise VlError(f"No vault '{ref}' on this computer. Vaults here: {known}")

    def personal(self, owner: str) -> str | None:
        """Your personal vault's ID for an owner, if it's on disk."""
        vault_id = vlt.personal_id(owner, self.me) if self.me else None
        return vault_id if vault_id in self.vaults else None

    def rules(self) -> list[Rule]:
        return [*self.repos.values(), *self.folders.values()]


# ---------------------------------------------------------------- reading

def _err(key: str, message: str) -> VlError:
    return VlError(f"{contract(config_path())}: {key}: {message}")


def _table(value, key: str) -> dict:
    if not isinstance(value, dict):
        raise _err(key, "should be a table")
    return value


def _no_unknown_keys(table: dict, allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            raise _err(f"{where}.{key}" if where else key,
                       f"unknown key. Allowed: {', '.join(sorted(allowed)) or 'none'}")


def _int(value, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _err(key, "should be a whole number of seconds")
    return value


def _vault_ref(value, key: str) -> str:
    if not isinstance(value, str) or not vlt.REPO_ID_RE.match(value.strip()):
        raise _err(key, f"should be one vault, written OWNER/REPO like \"acme/vault-public\", not {value!r}")
    return value.strip().lower()


def _rule(key: str, table: dict, where: str, allowed: set[str]) -> Rule:
    _no_unknown_keys(table, allowed, where)
    writes = _vault_ref(table["writes"], f"{where}.writes") if "writes" in table else None
    reads = table.get("reads", [])
    if not isinstance(reads, list):
        raise _err(f"{where}.reads", 'should be a list of vaults, like ["kabir/vault-recipes"]')
    reads = list(dict.fromkeys(_vault_ref(r, f"{where}.reads") for r in reads))
    if writes and writes in reads:
        raise _err(f"{where}.reads", f"'{writes}' is the vault notes are saved to here, so it can't also be in reads")
    flags = {}
    for flag in ("auto_pull", "dangerously_skip_hook_guards"):
        flags[flag] = table.get(flag, False)
        if not isinstance(flags[flag], bool):
            raise _err(f"{where}.{flag}", "should be true or false")
    return Rule(key, writes, reads, **flags)


def load_file() -> Config:
    """config.toml alone. Missing: the defaults."""
    path = config_path()
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise VlError(f"{contract(path)}: {e}") from None
    _no_unknown_keys(data, {*SETTINGS, "repos", "folders"}, "")

    cfg = Config(
        sync_interval=_int(data.get("sync_interval", SETTINGS["sync_interval"]), "sync_interval"),
        check_interval=_int(data.get("check_interval", SETTINGS["check_interval"]), "check_interval"),
        on_leak=data.get("on_leak", SETTINGS["on_leak"]),
        basic_memory=data.get("basic_memory", SETTINGS["basic_memory"]),
    )
    if cfg.on_leak not in ON_LEAK:
        raise _err("on_leak", f'should be "ask" or "block", not {cfg.on_leak!r}')
    if not isinstance(cfg.basic_memory, bool):
        raise _err("basic_memory", "should be true or false")

    for repo, table in _table(data.get("repos", {}), "repos").items():
        key = f'repos."{repo}"'
        if not REPO_KEY_RE.match(repo):
            raise _err(key, 'should be a repo, OWNER/REPO, or every repo of an owner, OWNER/*')
        rule = _rule(repo.lower(), _table(table, key), key, RULE_KEYS)
        if rule.auto_pull and repo.endswith("/*"):
            raise _err(f"{key}.auto_pull", "auto_pull needs one repo, not OWNER/*")
        cfg.repos[rule.key] = rule

    for text, table in _table(data.get("folders", {}), "folders").items():
        key = f'folders."{text}"'
        if not text.strip() or "*" in text:
            raise _err(key, 'should be a folder, like "~/Documents/writing"')
        rule = _rule(str(expand(text)), _table(table, key), key, FOLDER_KEYS)
        if rule.key in cfg.folders:
            raise _err(key, f"the same folder as another entry ({contract(rule.key)})")
        cfg.folders[rule.key] = rule
    return cfg


def load() -> Config:
    """config.toml, plus the vaults and owners on disk, plus who you are."""
    from .audience import cached_me

    cfg = load_file()
    cfg.vaults = vlt.on_disk()
    cfg.owners = vlt.joined()
    cfg.me = cached_me()
    return cfg


# ---------------------------------------------------------------- writing (only adding)

def write_template(basic_memory: bool = True) -> bool:
    """Write the commented config.toml, if there's none. Returns True if it wrote one."""
    path = config_path()
    if path.exists():
        if not basic_memory:
            set_basic_memory(False)
        return False
    text = TEMPLATE if basic_memory else TEMPLATE.replace(BASIC_MEMORY_LINE, "basic_memory = false   # don't set up Basic Memory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def set_basic_memory(on: bool) -> None:
    set_setting("basic_memory", "true" if on else "false   # don't set up Basic Memory")


def set_setting(key: str, value: str) -> None:
    """Set a setting (`value` in TOML) in place, keeping the rest of the file."""
    path = config_path()
    text = path.read_text()
    line = f"{key} = {value}"
    pattern = re.compile(rf"^#?\s*{key}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        # Settings must come before the first table, or TOML puts them in it.
        first = re.search(r"^\[", text, re.MULTILINE)
        at = first.start() if first else len(text)
        text = text[:at] + line + "\n" + ("\n" if first else "") + text[at:]
    path.write_text(text)
    load_file()  # still valid
