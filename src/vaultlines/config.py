"""The config file: ~/.config/vaultlines/config.toml."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from . import plugins
from .gitsync import check_remote
from .util import VlError, closest_parent, contract, expand, home

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SETTINGS_KEYS = {"vaults_dir", "sync_interval", "check_interval", "on_leak"}
VAULT_KEYS = {"path", "remote"}
FOLDER_KEYS = {"writes", "reads", "auto_pull"}
ON_LEAK = ("ask", "block")
DEFAULTS = {"vaults_dir": "~/Vaults", "sync_interval": 600, "check_interval": 86400, "on_leak": "ask"}


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
    path: str  # an absolute path
    writes: str
    reads: list[str] = field(default_factory=list)
    auto_pull: bool = False

    @property
    def vaults(self) -> list[str]:
        return [self.writes, *self.reads]


@dataclass
class Config:
    vaults_dir: Path = field(default_factory=lambda: expand(DEFAULTS["vaults_dir"]))
    sync_interval: int = DEFAULTS["sync_interval"]
    check_interval: int = DEFAULTS["check_interval"]
    on_leak: str = DEFAULTS["on_leak"]
    vaults: dict[str, Vault] = field(default_factory=dict)
    folders: dict[str, Folder] = field(default_factory=dict)  # folder path -> entry
    plugins: dict[str, dict] = field(default_factory=dict)  # plugin name -> its table, with `kind`

    def vault(self, name: str) -> Vault:
        if name not in self.vaults:
            known = ", ".join(sorted(self.vaults)) or "none"
            raise VlError(f"No vault named '{name}'. Known vaults: {known}")
        return self.vaults[name]

    def listed(self) -> list[Folder]:
        return [self.folders[p] for p in sorted(self.folders)]

    def users(self, vault: str) -> list[str]:
        """Folders that write or read a vault."""
        return [p for p, f in self.folders.items() if vault in f.vaults]


def folder_for(cfg: Config, cwd: str | Path) -> Folder | None:
    """The entry for a folder: the closest listed parent, or None."""
    key = closest_parent(cfg.folders, str(expand(cwd)))
    return cfg.folders[key] if key else None


def folder_key(text: str) -> str:
    return str(expand(text))


def show(folder: str) -> str:
    return contract(folder)


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


def _int(value, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _err(key, "should be a whole number of seconds")
    return value


def load() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise VlError(f"{contract(path)}: {e}") from None
    _no_unknown_keys(data, {"settings", "vaults", "folders", "plugins"}, "")

    s = _table(data.get("settings", {}), "settings")
    _no_unknown_keys(s, SETTINGS_KEYS, "settings")
    cfg = Config(
        vaults_dir=expand(s.get("vaults_dir", DEFAULTS["vaults_dir"])),
        sync_interval=_int(s.get("sync_interval", DEFAULTS["sync_interval"]), "settings.sync_interval"),
        check_interval=_int(s.get("check_interval", DEFAULTS["check_interval"]), "settings.check_interval"),
        on_leak=s.get("on_leak", DEFAULTS["on_leak"]),
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
        if text == "*":
            raise _err(key, 'there is no "*" any more: use "~" to cover every folder in your home folder.')
        _no_unknown_keys(_table(f, key), FOLDER_KEYS, key)
        if not isinstance(f.get("writes"), str):
            raise _err(f"{key}.writes", "missing. Every folder needs the one vault it writes to.")
        reads = f.get("reads", [])
        if not isinstance(reads, list) or not all(isinstance(r, str) for r in reads):
            raise _err(f"{key}.reads", "should be a list of vault names")
        if not isinstance(f.get("auto_pull", False), bool):
            raise _err(f"{key}.auto_pull", "should be true or false")
        folder = Folder(folder_key(text), f["writes"], list(dict.fromkeys(reads)), f.get("auto_pull", False))
        if folder.path in cfg.folders:
            raise _err(key, f"the same folder as another entry ({contract(folder.path)})")
        cfg.folders[folder.path] = folder

    for name, p in _table(data.get("plugins", {}), "plugins").items():
        key = f"plugins.{name}"
        kind = _table(p, key).get("kind")
        if kind is None:
            raise _err(f"{key}.kind", f'missing. Every plugin needs a kind, like kind = "{next(iter(plugins.KINDS))}"')
        if kind not in plugins.KINDS:
            raise _err(f"{key}.kind", f"unknown kind {kind!r}. Built in: {', '.join(sorted(plugins.KINDS))}")
        module = plugins.KINDS[kind]
        _no_unknown_keys(p, {"kind", *getattr(module, "KEYS", ()),
                             *(plugins.SOURCE_KEYS if hasattr(module, "run") else ())}, key)
        cfg.plugins[name] = dict(p)

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Raise a VlError naming the file and key of the first problem."""
    if cfg.on_leak not in ON_LEAK:
        raise _err("settings.on_leak", f'should be "ask" or "block", not {cfg.on_leak!r}')
    seen: dict[str, str] = {}
    for name, p in sorted(cfg.plugins.items()):
        module = plugins.KINDS[p["kind"]]
        if getattr(module, "TOOL_PREFIXES", ()) and p["kind"] in seen:
            raise _err(f"plugins.{name}", f"only one {p['kind']} plugin can be on; '{seen[p['kind']]}' is too")
        seen[p["kind"]] = name
        problem = module.validate(p) if hasattr(module, "validate") else None
        if problem:
            raise _err(f"plugins.{name}.{problem[0]}", problem[1])
    for name, v in cfg.vaults.items():
        if not NAME_RE.match(name):
            raise _err(f"vaults.{name}", "vault names use lowercase letters, digits and dashes")
        if v.remote:
            try:
                check_remote(v.remote)
            except VlError as e:
                raise _err(f"vaults.{name}.remote", str(e)) from None
    for f in cfg.folders.values():
        key = f'folders."{show(f.path)}"'
        if f.writes not in cfg.vaults:
            raise _err(f"{key}.writes", f"no vault named '{f.writes}'")
        for r in f.reads:
            if r not in cfg.vaults:
                raise _err(f"{key}.reads", f"no vault named '{r}'")
        if f.writes in f.reads:
            raise _err(f"{key}.reads", f"'{f.writes}' is the vault this folder writes to, so it can't also be in reads")
    filled: dict[str, str] = {}
    for name, _, p in plugins.sources(cfg):
        key = f"plugins.{name}"
        vault = p.get("vault")
        if vault is None:
            raise _err(f"{key}.vault", 'missing. A source fills one vault, like vault = "NAME"')
        if not isinstance(vault, str):
            raise _err(f"{key}.vault", "should be a vault name")
        if vault not in cfg.vaults:
            raise _err(f"{key}.vault", f"no vault named '{vault}'")
        if vault in filled:
            raise _err(f"{key}.vault", f"'{vault}' is already filled by plugin '{filled[vault]}'. A vault has one source.")
        filled[vault] = name
        if "every" in p:
            _int(p["every"], f"{key}.every")
    for f in cfg.listed():
        if f.writes in filled:
            raise _err(f'folders."{show(f.path)}".writes',
                       f"'{f.writes}' is filled by plugin '{filled[f.writes]}', and each run replaces its files, "
                       "so notes can't be written there. Put it in reads instead.")


def save(cfg: Config) -> None:
    validate(cfg)
    settings: dict = {"sync_interval": cfg.sync_interval, "check_interval": cfg.check_interval,
                      "on_leak": cfg.on_leak}
    if contract(cfg.vaults_dir) != DEFAULTS["vaults_dir"]:
        settings["vaults_dir"] = contract(cfg.vaults_dir)
    vaults = {}
    for name, v in sorted(cfg.vaults.items()):
        vaults[name] = {"path": contract(v.path), **({"remote": v.remote} if v.remote else {})}
    folders = {}
    for f in cfg.listed():
        entry: dict = {"writes": f.writes}
        if f.reads:
            entry["reads"] = f.reads
        if f.auto_pull:
            entry["auto_pull"] = True
        folders[contract(f.path)] = entry
    data = {"settings": settings, "vaults": vaults, "folders": folders,
            "plugins": {name: dict(p) for name, p in sorted(cfg.plugins.items())}}
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".vl-tmp")
    tmp.write_text("# Managed by vaultlines (vl). Edit freely, then run `vl apply`.\n\n" + tomli_w.dumps(data))
    tmp.replace(path)
