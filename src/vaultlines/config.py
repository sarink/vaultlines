"""The config file: ~/.config/vaultlines/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from .util import VlError, contract, expand, home

LEVELS = ("personal", "private", "public")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(home() / ".config")
    return Path(base) / "vaultlines"


def config_path() -> Path:
    return Path(os.environ.get("VAULTLINES_CONFIG") or config_dir() / "config.toml")


@dataclass
class Vault:
    name: str
    path: Path
    level: str
    team: str | None = None
    remote: str | None = None


@dataclass
class Config:
    vaults_dir: Path = field(default_factory=lambda: expand("~/Vaults"))
    sync_interval: int = 600
    github_owner: str | None = None
    default_vault: str | None = None
    bm_command: str = "uvx basic-memory"
    vaults: dict[str, Vault] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)  # folder path -> vault name
    follow: list[str] = field(default_factory=list)  # repos to keep pulled (ff-only)

    def vault(self, name: str) -> Vault:
        if name not in self.vaults:
            known = ", ".join(sorted(self.vaults)) or "none"
            raise VlError(f"No vault named '{name}'. Known vaults: {known}")
        return self.vaults[name]


def load() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    s = data.get("settings", {})
    cfg = Config(
        vaults_dir=expand(s.get("vaults_dir", "~/Vaults")),
        sync_interval=int(s.get("sync_interval", 600)),
        github_owner=s.get("github_owner"),
        default_vault=s.get("default_vault"),
        bm_command=s.get("bm_command", "uvx basic-memory"),
        bindings={str(expand(k)): v for k, v in data.get("bindings", {}).items()},
        follow=[str(expand(p)) for p in data.get("follow", {}).get("repos", [])],
    )
    for name, v in data.get("vaults", {}).items():
        if v.get("level") not in LEVELS:
            raise VlError(f"Vault '{name}' in {path} has an unknown level: {v.get('level')}")
        cfg.vaults[name] = Vault(
            name=name,
            path=expand(v["path"]),
            level=v["level"],
            team=v.get("team"),
            remote=v.get("remote"),
        )
    return cfg


def save(cfg: Config) -> None:
    settings: dict = {
        "vaults_dir": contract(cfg.vaults_dir),
        "sync_interval": cfg.sync_interval,
        "bm_command": cfg.bm_command,
    }
    if cfg.github_owner:
        settings["github_owner"] = cfg.github_owner
    if cfg.default_vault:
        settings["default_vault"] = cfg.default_vault
    vaults = {}
    for name, v in sorted(cfg.vaults.items()):
        entry = {"path": contract(v.path), "level": v.level}
        if v.team:
            entry["team"] = v.team
        if v.remote:
            entry["remote"] = v.remote
        vaults[name] = entry
    data = {
        "settings": settings,
        "vaults": vaults,
        "bindings": {contract(k): v for k, v in sorted(cfg.bindings.items())},
        "follow": {"repos": [contract(p) for p in cfg.follow]},
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".vl-tmp")
    tmp.write_text("# Managed by vaultlines (vl). Edit freely, then run `vl apply`.\n\n" + tomli_w.dumps(data))
    tmp.replace(path)
