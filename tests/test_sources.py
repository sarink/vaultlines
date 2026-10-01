"""Sources: plugins that fill a vault, on a timer."""

import json
import socket
import subprocess

import pytest

from vaultlines import gitsync
from vaultlines.config import Config, Vault
from vaultlines.plugins import claim, drive, due, every, owner, sources
from vaultlines.util import VlError, machine_id


def test_due():
    assert due(None, 3600, 1000)
    assert not due(1000, 3600, 1000)
    assert not due(1000, 3600, 4599)
    assert due(1000, 3600, 4600)


def test_sources_and_every(tmp_path):
    cfg = Config()
    cfg.vaults["drive"] = Vault("drive", tmp_path)
    cfg.plugins = {"basic-memory": {"kind": "basic-memory"},
                   "mixim": {"kind": "drive", "vault": "drive", "remote": "mixim:"},
                   "legal": {"kind": "drive", "vault": "drive", "remote": "mixim:Legal", "every": 60}}
    found = sources(cfg)
    assert [(name, module) for name, module, _ in found] == [("legal", drive), ("mixim", drive)]
    assert [every(module, s) for _, module, s in found] == [60, drive.EVERY]


def test_commit_only_when_something_changed(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True)
    (tmp_path / "a.md").write_text("a\n")
    assert gitsync.commit(tmp_path, "Update from drive") is True
    assert gitsync.git(tmp_path, "log", "-1", "--format=%s").stdout.strip() == "Update from drive"
    assert gitsync.commit(tmp_path, "Update from drive") is False
    assert gitsync.git(tmp_path, "rev-list", "--count", "HEAD").stdout.strip() == "1"


# ---------------------------------------------------------------- one computer per source


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_machine_id_is_made_once(state):
    first = machine_id()
    assert len(first) >= 16 and machine_id() == first


def test_the_first_run_claims_the_vault(tmp_path, state):
    claim(tmp_path, "mixim-drive", "mixim-drive", take_over=False)
    assert json.loads((tmp_path / ".vl-source").read_text()) == {
        "source": "mixim-drive", "machine": machine_id(), "host": socket.gethostname().split(".")[0]}
    assert owner(tmp_path)["machine"] == machine_id()
    claim(tmp_path, "mixim-drive", "mixim-drive", take_over=False)  # this computer again: fine


def test_another_computer_is_refused_until_it_takes_over(tmp_path, state):
    (tmp_path / ".vl-source").write_text(json.dumps({"source": "mixim-drive", "machine": "other", "host": "bobs-mac"}))
    with pytest.raises(VlError) as e:
        claim(tmp_path, "mixim-drive", "mixim-drive", take_over=False)
    assert str(e.value) == ("mixim-drive is filled from bobs-mac. To move it here, "
                            "run `vl sync mixim-drive --take-over`.")
    claim(tmp_path, "mixim-drive", "mixim-drive", take_over=True)
    assert owner(tmp_path)["machine"] == machine_id()


def test_no_owner_yet(tmp_path):
    assert owner(tmp_path) is None
    (tmp_path / ".vl-source").write_text("not json")
    assert owner(tmp_path) is None
