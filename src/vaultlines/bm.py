"""Basic Memory: one project per vault."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from .config import Config
from .util import VlError, run


def _cmd(cfg: Config) -> list[str]:
    return shlex.split(cfg.bm_command)


def mcp_argv(cfg: Config, project: str) -> list[str]:
    return [*_cmd(cfg), "mcp", "--project", project]


def projects(cfg: Config) -> dict[str, Path]:
    out = run([*_cmd(cfg), "tool", "list-projects"]).stdout
    start = out.find("{")
    if start < 0:
        raise VlError(f"Couldn't read Basic Memory projects:\n{out}")
    data = json.JSONDecoder().raw_decode(out[start:])[0]
    return {p["name"]: Path(p["path"]).resolve() for p in data.get("projects", [])}


def ensure_project(cfg: Config, name: str, path: Path) -> None:
    path = path.resolve()
    current = projects(cfg)
    if current.get(name) == path:
        return
    if name in current:
        raise VlError(
            f"Basic Memory already has a project '{name}' at {current[name]}, not {path}. "
            f"Remove it with `{cfg.bm_command} project remove {name}` or pick another name."
        )
    for other, other_path in current.items():
        if other_path == path:
            # Same folder under another name: re-register it under the vault name.
            run([*_cmd(cfg), "project", "remove", other])
    run([*_cmd(cfg), "project", "add", name, str(path)])


def remove_project(cfg: Config, name: str) -> None:
    if name in projects(cfg):
        run([*_cmd(cfg), "project", "remove", name])


def set_default(cfg: Config, name: str) -> None:
    run([*_cmd(cfg), "project", "default", name])


def configure(cfg: Config) -> None:
    """Stop Basic Memory from rewriting notes that other people also edit."""
    run([*_cmd(cfg), "config", "set", "disable_permalinks", "true"])
    run([*_cmd(cfg), "config", "set", "ensure_frontmatter_on_sync", "false"])


def setting(cfg: Config, key: str) -> str:
    return run([*_cmd(cfg), "config", "get", key], check=False).stdout.strip()
