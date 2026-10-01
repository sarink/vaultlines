"""A fake home folder with vaults, listed folders and a runtime.json, for hook tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ME = "sam"
PEOPLE = {
    "personal": {"kind": "people", "logins": ["sam"]},
    "acme-founders": {"kind": "people", "logins": ["sam", "Lee"]},
    "acme-everyone": {"kind": "people", "logins": ["sam", "Lee", "ana"]},
    "acme-slack": {"kind": "people", "logins": ["sam", "Lee", "ana"]},
    "acme": {"kind": "people", "logins": ["sam", "Lee"]},  # a prefix of the two above
    "side": {"kind": "me"},
    "handbook": {"kind": "unknown", "reason": "you have read-only access"},
    "docs": {"kind": "everyone"},
}


class World:
    def __init__(self, root: Path):
        self.home = root / "home"
        self.vaults = self.home / "Vaults"
        self.acme = self.home / "sam" / "Projects" / "acme"
        self.site = self.acme / "site"
        self.api = self.acme / "api"
        self.blog = self.home / "sam" / "Projects" / "blog"
        self.side_project = self.home / "side"
        for d in (self.site, self.api, self.blog, self.side_project):
            d.mkdir(parents=True)
        for name in PEOPLE:
            (self.vaults / name).mkdir(parents=True)
        self.runtime = {
            "version": 3,
            "written_at": 0,
            "me": ME,
            "on_leak": "ask",
            "vaults": {name: {"paths": [str(self.vaults / name)], "show": f"~/Vaults/{name}",
                              "audience": {"logins": [], "reason": "", **copy.deepcopy(aud)}}
                       for name, aud in PEOPLE.items()},
            "folders": {
                str(self.home): {"writes": "personal", "reads": ["docs"]},
                str(self.acme): {"writes": "acme-founders", "reads": ["acme-everyone", "acme-slack"]},
                str(self.site): {"writes": "acme-everyone", "reads": ["acme-slack"]},
                str(self.side_project): {"writes": "acme-founders", "reads": ["side", "handbook", "acme"]},
            },
            "plugins": {
                "basic-memory": {
                    "kind": "basic-memory",
                    "tool_prefixes": ["mcp__basic-memory__"],
                    "data": {"plugin": True, "projects": {**{name: name for name in PEOPLE}, "main": None}},
                },
            },
        }

    def vault(self, name: str, *rest: str) -> str:
        return str(self.vaults.joinpath(name, *rest))


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    w = World(tmp_path.resolve())
    monkeypatch.setenv("HOME", str(w.home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path.resolve() / "state"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return w


def write_runtime(world: World) -> None:
    from vaultlines.hook import runtime_path

    runtime_path().parent.mkdir(parents=True, exist_ok=True)
    runtime_path().write_text(json.dumps(world.runtime))
