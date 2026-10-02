"""github.py with the fake GitHub tests use: a JSON file plus bare repos on disk."""

import json
import subprocess

import pytest

from vaultlines import github
from vaultlines.util import VlError


@pytest.fixture
def fake(fake_github):
    gh = fake_github
    gh.org("mixim-ai", ["alice", "bob"])
    gh.vault("mixim-ai/vault-public", ["alice", "bob"])
    gh.vault("mixim-ai/vault-private", ["alice"])
    gh.vault("mixim-ai/vault-hq", {"push": ["alice"], "read": ["bob"]})
    gh.repo("mixim-ai/vault-notoml", ["alice", "bob"], {"README.md": "x"})
    gh.repo("mixim-ai/marketing", ["alice", "bob"], {"README.md": "x"})
    gh.vault("alice/vault-alice-personal", ["alice"])
    gh.vault("carol/vault-open", "public")
    return gh.root, gh.path


def test_login(fake):
    assert github.login() == "alice"


def test_discovery_needs_the_prefix_and_vault_toml(fake):
    root, _ = fake
    assert github.vault_repos("mixim-ai") == {
        "mixim-ai/vault-hq": f"file://{root}/mixim-ai/vault-hq.git",
        "mixim-ai/vault-private": f"file://{root}/mixim-ai/vault-private.git",
        "mixim-ai/vault-public": f"file://{root}/mixim-ai/vault-public.git",
    }
    assert sorted(github.vault_repos("alice")) == ["alice/vault-alice-personal"]


def test_discovery_only_shows_repos_you_can_access(fake, monkeypatch):
    monkeypatch.setenv("VAULTLINES_FAKE_LOGIN", "bob")
    assert sorted(github.vault_repos("mixim-ai")) == ["mixim-ai/vault-hq", "mixim-ai/vault-public"]
    assert sorted(github.vault_repos("carol")) == ["carol/vault-open"]  # public


def test_owner_kind(fake):
    assert github.owner_kind("mixim-ai") == "Organization"
    assert github.owner_kind("alice") == "User"
    assert github.owner_kind("nobody-here") is None


def test_creating_a_repo_pushes_it_and_only_you_can_see_it(fake, tmp_path):
    root, path = fake
    work = tmp_path / "new"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "vault.toml").write_text('about = "mine"\n')
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    url = github.create_private_repo("mixim-ai/vault-alice-personal", work)
    assert url == f"file://{root}/mixim-ai/vault-alice-personal.git"
    assert subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"], capture_output=True,
                          text=True).stdout.strip() == url
    assert json.loads(path.read_text())["repos"]["mixim-ai/vault-alice-personal"] == {"push": ["alice"]}
    assert "mixim-ai/vault-alice-personal" in github.vault_repos("mixim-ai")
    with pytest.raises(VlError, match="already exists"):
        github.create_private_repo("mixim-ai/vault-alice-personal", work)


def test_who_can_see_a_repo(fake, monkeypatch):
    assert github.audience("mixim-ai/vault-hq").to_json()["logins"] == ["alice", "bob"]
    assert github.audience("carol/vault-open").kind == "everyone"
    monkeypatch.setenv("VAULTLINES_FAKE_LOGIN", "bob")
    hq = github.audience("mixim-ai/vault-hq")
    assert hq.kind == "unknown" and "read-only" in hq.reason
    assert github.audience("mixim-ai/vault-private").kind == "unknown"  # no access at all


def test_secrets_and_workflow_runs_are_recorded(fake):
    _, path = fake
    github.set_secret("mixim-ai/vault-hq", "VL_SOURCE_TOKEN", "1//refresh")
    github.run_workflow("mixim-ai/vault-hq", "vl-source.yml", {"force": "true"})
    data = json.loads(path.read_text())
    assert data["secrets"] == {"mixim-ai/vault-hq": {"VL_SOURCE_TOKEN": "1//refresh"}}
    assert data["workflow_runs"] == [{"repo": "mixim-ai/vault-hq", "workflow": "vl-source.yml",
                                      "inputs": {"force": "true"}}]


def test_real_secret_value_goes_through_stdin(monkeypatch):
    monkeypatch.delenv("VAULTLINES_FAKE_GITHUB", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    github.set_secret("mixim-ai/vault-hq", "VL_SOURCE_TOKEN", "1//SECRET")
    cmd, kw = calls[0]
    assert cmd == ["gh", "secret", "set", "VL_SOURCE_TOKEN", "--repo", "mixim-ai/vault-hq"]
    assert kw["input"] == "1//SECRET" and "1//SECRET" not in " ".join(cmd)
