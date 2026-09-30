"""The config file: ~/.config/vaultlines/config.toml."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from .gitsync import check_remote
from .util import VlError, contract, expand, home

STAR = "*"  # the folder entry for every folder that isn't listed
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SETTINGS_KEYS = {"vaults_dir", "sync_interval", "bm_command"}
VAULT_KEYS = {"path", "remote"}
FOLDER_KEYS = {"writes", "reads", "auto_pull"}
DEFAULTS = {"vaults_dir": "~/Vaults", "sync_interval": 600, "bm_command": "uvx basic-memory"}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(home() / ".config")
    return Path(base) / "vaultlines"


def config_path() -> Path:
    return Path(os.environ.get("VAULTLINES_CONFIG") or config_dir() / "config.toml")


@dataclass
class Vault:
    name: str
    path: Path
    remote: str | None = None  # a GitHub URL; None = this computer only


@dataclass
class Folder:
    path: str  # an absolute path, or "*"
    writes: str
    reads: list[str] = field(default_factory=list)
    auto_pull: bool = False


@dataclass
class Config:
    vaults_dir: Path = field(default_factory=lambda: expand(DEFAULTS["vaults_dir"]))
    sync_interval: int = DEFAULTS["sync_interval"]
    bm_command: str = DEFAULTS["bm_command"]
    vaults: dict[str, Vault] = field(default_factory=dict)
    folders: dict[str, Folder] = field(default_factory=dict)  # folder path (or "*") -> entry

    def vault(self, name: str) -> Vault:
        if name not in self.vaults:
            known = ", ".join(sorted(self.vaults)) or "none"
            raise VlError(f"No vault named '{name}'. Known vaults: {known}")
        return self.vaults[name]

    @property
    def star(self) -> Folder | None:
        return self.folders.get(STAR)

    def listed(self) -> list[Folder]:
        """Every folder entry except "*"."""
        return [f for p, f in sorted(self.folders.items()) if p != STAR]

    def users(self, vault: str) -> list[str]:
        """Folders that write or read a vault."""
        return [p for p, f in self.folders.items() if vault == f.writes or vault in f.reads]


def folder_key(text: str) -> str:
    return STAR if text == STAR else str(expand(text))


def show(folder: str) -> str:
    return '"*"' if folder == STAR else contract(folder)


def _err(key: str, message: str) -> VlError:
    return VlError(f"{contract(config_path())}: {key}: {message}")


def _no_unknown_keys(table: dict, allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            raise _err(f"{where}.{key}" if where else key,
                       f"unknown key. Allowed: {', '.join(sorted(allowed))}")


def _table(value, key: str) -> dict:
    if not isinstance(value, dict):
        raise _err(key, "should be a table")
    return value


def load() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise VlError(f"{contract(path)}: {e}") from None
    _no_unknown_keys(data, {"settings", "vaults", "folders"}, "")

    s = _table(data.get("settings", {}), "settings")
    _no_unknown_keys(s, SETTINGS_KEYS, "settings")
    cfg = Config(
        vaults_dir=expand(s.get("vaults_dir", DEFAULTS["vaults_dir"])),
        sync_interval=int(s.get("sync_interval", DEFAULTS["sync_interval"])),
        bm_command=s.get("bm_command", DEFAULTS["bm_command"]),
    )

    for name, v in _table(data.get("vaults", {}), "vaults").items():
        key = f"vaults.{name}"
        _no_unknown_keys(_table(v, key), VAULT_KEYS, key)
        if not isinstance(v.get("path"), str):
            raise _err(f"{key}.path", "missing. Every vault needs a path.")
        remote = v.get("remote")
        if remote is not None and not isinstance(remote, str):
            raise _err(f"{key}.remote", "should be a URL")
        cfg.vaults[name] = Vault(name, expand(v["path"]), remote)

    for text, f in _table(data.get("folders", {}), "folders").items():
        key = f'folders."{text}"'
        _no_unknown_keys(_table(f, key), FOLDER_KEYS, key)
        for required in ("writes", "reads"):
            if required not in f:
                raise _err(f"{key}.{required}", "missing. Every folder needs `writes` and `reads` (reads can be []).")
        if not isinstance(f["writes"], str):
            raise _err(f"{key}.writes", "should be one vault name")
        if not isinstance(f["reads"], list) or not all(isinstance(r, str) for r in f["reads"]):
            raise _err(f"{key}.reads", "should be a list of vault names")
        if not isinstance(f.get("auto_pull", False), bool):
            raise _err(f"{key}.auto_pull", "should be true or false")
        folder = Folder(folder_key(text), f["writes"], list(dict.fromkeys(f["reads"])), f.get("auto_pull", False))
        if folder.path in cfg.folders:
            raise _err(key, f"the same folder as another entry ({contract(folder.path)})")
        cfg.folders[folder.path] = folder

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Raise a VlError naming the file and key of the first problem."""
    for name, v in cfg.vaults.items():
        if not NAME_RE.match(name):
            raise _err(f"vaults.{name}", "vault names use lowercase letters, digits and dashes")
        if v.remote:
            try:
                check_remote(v.remote)
            except VlError as e:
                raise _err(f"vaults.{name}.remote", str(e)) from None
    for f in cfg.folders.values():
        key = f"folders.{show(f.path)}"
        if f.writes not in cfg.vaults:
            raise _err(f"{key}.writes", f"no vault named '{f.writes}'")
        for r in f.reads:
            if r not in cfg.vaults:
                raise _err(f"{key}.reads", f"no vault named '{r}'")
        if f.writes in f.reads:
            raise _err(f"{key}.reads", f"'{f.writes}' is the vault this folder writes to, so it can't also be in reads")
        if f.auto_pull and f.path == STAR:
            raise _err(f"{key}.auto_pull", 'only works on a listed folder, not "*"')
    star = cfg.star
    if star:
        for f in cfg.listed():
            if f.writes in star.reads:
                raise _err(
                    f"folders.{show(f.path)}.writes",
                    f"'{f.writes}' is in folders.\"*\".reads, so writing to it asks first in every folder, "
                    f"including this one (Claude Code applies user-level ask rules everywhere). "
                    f"Remove '{f.writes}' from folders.\"*\".reads, or add it only to the folders that need it.",
                )


def save(cfg: Config) -> None:
    validate(cfg)
    settings: dict = {"sync_interval": cfg.sync_interval}
    if contract(cfg.vaults_dir) != DEFAULTS["vaults_dir"]:
        settings["vaults_dir"] = contract(cfg.vaults_dir)
    if cfg.bm_command != DEFAULTS["bm_command"]:
        settings["bm_command"] = cfg.bm_command
    vaults = {}
    for name, v in sorted(cfg.vaults.items()):
        vaults[name] = {"path": contract(v.path), **({"remote": v.remote} if v.remote else {})}
    folders = {}
    for path in sorted(cfg.folders, key=lambda p: (p != STAR, p)):
        f = cfg.folders[path]
        entry: dict = {"writes": f.writes, "reads": f.reads}
        if f.auto_pull:
            entry["auto_pull"] = True
        folders[STAR if path == STAR else contract(path)] = entry
    data = {"settings": settings, "vaults": vaults, "folders": folders}
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".vl-tmp")
    tmp.write_text("# Managed by vaultlines (vl). Edit freely, then run `vl apply`.\n\n" + tomli_w.dumps(data))
    tmp.replace(path)
