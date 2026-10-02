"""`vl migrate`: a vl 0.3 setup moves into ~/.vaultlines once."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import vl

from vaultlines import config, obsidian, runtime, vaults
from vaultlines.util import clones_path, vaults_dir


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def repo(path, origin=None):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", str(path))
    if origin:
        git("-C", str(path), "remote", "add", "origin", origin)
    return path


def vault(path, files, origin=None):
    repo(path, origin)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git("-C", str(path), "add", "-A")
    git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "notes")
    return path


OLD_CONFIG = """# Managed by vaultlines (vl). Edit freely, then run `vl apply`.

[settings]
sync_interval = 300
check_interval = 86400
on_leak = "ask"

[vaults.personal]
path = "~/Vaults/personal"

[vaults.mixim-private]
path = "~/Vaults/mixim-private"

[vaults.mixim-public]
path = "~/code/mixim/workspace/mixim-public"
remote = "{public}"

[vaults.old-team]
path = "~/Vaults/old-team"
remote = "{team}"

[folders."~"]
writes = "personal"

[folders."~/code/mixim"]
writes = "mixim-private"
reads = ["mixim-public"]

[folders."~/code/mixim/marketing"]
writes = "mixim-public"

[folders."~/code/mixim/workspace"]
writes = "mixim-public"
auto_pull = true

[folders."~/code/acme-app"]
writes = "old-team"

[folders."~/gone"]
writes = "personal"
"""


@pytest.fixture
def old(fake_github, computer, monkeypatch):
    """A vl 0.3 computer. mixim-public was renamed to vault-public on GitHub, with a vault.toml."""
    home = computer
    gh = fake_github
    gh.org("mixim-ai", ["alice"])
    gh.vault("mixim-ai/vault-public", ["alice"], 'about = "Everyone."\n')
    gh.vault("mixim-ai/vault-design", ["alice"])  # a vault 0.3 didn't have here
    data = gh.load()
    data["renames"] = {"mixim-ai/mixim-public": "mixim-ai/vault-public"}
    gh.save(data)
    gh.repo("acme/team-notes", ["alice"], {"a.md": "x"})
    config.config_path().unlink()  # the computer fixture made a 0.4 one
    monkeypatch.setattr(obsidian, "running", lambda: False)

    vault(home / "Vaults" / "personal", {"me.md": "mine\n"})
    vault(home / "Vaults" / "mixim-private", {"plan.md": "secret\n"})
    vault(home / "Vaults" / "old-team", {"t.md": "t\n"}, gh.url("acme/team-notes"))
    workspace = repo(home / "code" / "mixim" / "workspace", "https://github.com/mixim-ai/mixim-workspace.git")
    git("clone", "-q", gh.url("mixim-ai/vault-public"), str(workspace / "mixim-public"))
    git("-C", str(workspace / "mixim-public"), "remote", "set-url", "origin",
        f"file://{gh.root}/mixim-ai/mixim-public.git")  # its name before the rename
    repo(home / "code" / "mixim" / "marketing", "git@github.com:mixim-ai/marketing.git")
    repo(home / "code" / "acme-app", "https://github.com/acme/app.git")

    conf = home / ".config" / "vaultlines"
    conf.mkdir(parents=True)
    (conf / "config.toml").write_text(OLD_CONFIG.format(public=f"file://{gh.root}/mixim-ai/mixim-public.git",
                                                        team=gh.url("acme/team-notes")))
    (conf / "state.json").write_text(json.dumps({"me": "alice", "plugins": {}}))
    state = home / ".local" / "state" / "vaultlines"
    (state / "sessions").mkdir(parents=True)
    (state / "sessions" / "s1.json").write_text("{}")
    (state / "runtime.json").write_text('{"version": 3}')
    (state / "machine-id").write_text("abc\n")
    obs = home / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    obs.parent.mkdir(parents=True)
    obs.write_text(json.dumps({"vaults": {"a1": {"path": str(home / "Vaults" / "personal"), "ts": 1}}}))
    return home


ARGS = ["migrate", "--map", "mixim-private=mixim-ai/vault-private"]


def test_dry_run_changes_nothing(old, capsys):
    assert vl(*ARGS, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "personal -> alice/vault-alice-personal" in out
    assert "mixim-public -> mixim-ai/vault-public" in out
    assert "mixim-private -> mixim-ai/vault-private (a new private repo on GitHub)" in out
    assert "old-team: needs an admin step" in out
    assert (old / "Vaults" / "personal" / "me.md").exists()
    assert not vaults_dir().exists()
    assert not config.config_path().exists()


def test_migrate(old, fake_github, capsys):
    assert vl(*ARGS) == 0
    out = capsys.readouterr().out
    found = vaults.on_disk()
    assert sorted(found) == ["alice/vault-alice-personal", "mixim-ai/vault-alice-personal", "mixim-ai/vault-design",
                             "mixim-ai/vault-private", "mixim-ai/vault-public"]
    # Notes moved, history kept.
    personal = vaults_dir() / "alice" / "vault-alice-personal"
    assert (personal / "me.md").read_text() == "mine\n"
    assert vaults.parse_vault_toml((personal / "vault.toml").read_text())[0].about == "alice's personal notes."
    assert not (old / "Vaults" / "personal").exists()
    public = found["mixim-ai/vault-public"]
    assert public.remote == fake_github.url("mixim-ai/vault-public")  # its name now
    assert not (old / "code" / "mixim" / "workspace" / "mixim-public").exists()
    private = found["mixim-ai/vault-private"]
    assert (private.path / "plan.md").read_text() == "secret\n"
    assert vaults.remote_id(private.remote) == "mixim-ai/vault-private"
    # The vault that doesn't follow the rules stays where it was.
    assert (old / "Vaults" / "old-team" / "t.md").exists()
    assert "old-team: needs an admin step" in out

    cfg = config.load_file()
    assert cfg.sync_interval == 300
    assert cfg.repos["mixim-ai/marketing"].writes == "mixim-ai/vault-public"
    workspace = cfg.repos["mixim-ai/mixim-workspace"]
    assert (workspace.writes, workspace.auto_pull) == ("mixim-ai/vault-public", True)
    parent = cfg.folders[str(old / "code" / "mixim")]
    assert (parent.writes, parent.reads) == ("mixim-ai/vault-private", ["mixim-ai/vault-public"])
    assert str(old) not in cfg.folders  # "~" is the default now
    assert not any("acme" in k for k in cfg.repos) and str(old / "code" / "acme-app") not in cfg.folders
    assert "# Your own changes." in config.config_path().read_text()
    assert "notes_from" in out and "mixim-ai/marketing" in out  # entries notes_from could replace

    assert json.loads(clones_path().read_text()) == {
        str((old / "code" / "mixim" / "marketing").resolve()): "mixim-ai/marketing",
        str((old / "code" / "mixim" / "workspace").resolve()): "mixim-ai/mixim-workspace"}
    assert (config.vl_home() / "state" / "sessions" / "s1.json").exists()
    for folder in (old / ".config" / "vaultlines", old / ".local" / "state" / "vaultlines", old / "Vaults"):
        assert "~/.vaultlines" in (folder / "MOVED.txt").read_text()
    obs = json.loads((old / "Library" / "Application Support" / "obsidian" / "obsidian.json").read_text())
    assert obs["vaults"]["a1"]["path"] == str(personal)
    data = runtime.load()
    assert data["default"]["writes"] == "alice-personal"
    assert data["repos"]["mixim-ai/marketing"]["writes"] == "mixim-ai-public"


def test_migrate_only_once(old, capsys):
    assert vl(*ARGS) == 0
    assert vl(*ARGS) == 1
    assert "already" in capsys.readouterr().err


def test_without_map_a_local_vault_becomes_local(old):
    assert vl("migrate") == 0
    assert "local/mixim-private" in vaults.on_disk()
    assert config.load_file().local == ["local/mixim-private"]
    assert config.load_file().folders[str(old / "code" / "mixim")].writes == "local/mixim-private"


def test_no_old_setup(fake_github, computer, capsys):
    assert vl("migrate") == 1
    assert "No vl 0.3 setup" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["nope=local/x", "mixim-private=mixim-ai/private", "mixim-private"])
def test_bad_maps(old, capsys, bad):
    assert vl("migrate", "--map", bad) == 1
    assert not vaults_dir().exists()


def test_entries_notes_from_already_covers_are_left_out(old, fake_github):
    work = fake_github.root / ".work" / "mixim-ai" / "vault-public"
    from conftest import commit_files

    commit_files(work, {"vault.toml": 'notes_from = ["mixim-ai/marketing", "mixim-ai/mixim-workspace"]\n'}, "nf")
    subprocess.run(["git", "-C", str(old / "code" / "mixim" / "workspace" / "mixim-public"), "pull", "-q",
                    fake_github.url("mixim-ai/vault-public"), "main"], check=True, capture_output=True)
    assert vl(*ARGS) == 0
    cfg = config.load_file()
    assert "mixim-ai/marketing" not in cfg.repos
    workspace = cfg.repos["mixim-ai/mixim-workspace"]
    assert (workspace.writes, workspace.auto_pull) == (None, True)  # kept for auto_pull only
    assert runtime.load()["repos"]["mixim-ai/mixim-workspace"] == {"writes": None, "reads": []}
