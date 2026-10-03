"""Obsidian's vault list: vl adds its vaults, and drops the ones that are gone from vaults/."""

from __future__ import annotations

import json

import pytest

from vaultlines import obsidian
from vaultlines.util import home, vaults_dir


@pytest.fixture
def listed(computer, monkeypatch):
    """Obsidian, closed, knowing a vault that moved away, a folder of its own, and a vault still there."""
    monkeypatch.setattr(obsidian, "installed", lambda: True)
    monkeypatch.setattr(obsidian, "running", lambda: False)
    here = vaults_dir() / "acme" / "vault-public"
    here.mkdir(parents=True)
    mine = home() / "Notes"
    mine.mkdir()
    obsidian._config().parent.mkdir(parents=True)
    obsidian._config().write_text(json.dumps({"vaults": {
        "a": {"path": str(vaults_dir() / "local" / "acme-private"), "ts": 1},
        "b": {"path": str(mine), "ts": 2},
        "c": {"path": str(here), "ts": 3, "open": True},
    }, "frame": "x"}))
    return here, mine


def paths():
    return sorted(v["path"] for v in json.loads(obsidian._config().read_text())["vaults"].values())


def test_register_adds_new_vaults_and_drops_gone_ones(listed):
    here, mine = listed
    new = vaults_dir() / "acme" / "vault-private"
    new.mkdir()
    assert obsidian.register([here, new]) == []
    assert paths() == sorted([str(here), str(mine), str(new)])  # folders outside vaults/ aren't vl's
    data = json.loads(obsidian._config().read_text())
    assert data["frame"] == "x" and data["vaults"]["c"]["open"] is True  # the rest is kept


def test_register_drops_gone_vaults_even_with_nothing_to_add(listed):
    here, mine = listed
    assert obsidian.register([here]) == []
    assert paths() == sorted([str(here), str(mine)])


def test_register_changes_nothing_while_obsidian_runs(listed, monkeypatch):
    here, _ = listed
    monkeypatch.setattr(obsidian, "running", lambda: True)
    before = obsidian._config().read_text()
    new = vaults_dir() / "acme" / "vault-private"
    assert obsidian.register([here, new]) == [new]
    assert obsidian._config().read_text() == before
