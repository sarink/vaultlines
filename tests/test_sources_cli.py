"""Vaults with a source: `vl vault create --source KIND`, and `vl source refresh/fetch/login`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest
from conftest import commit_files, vl
from fake_google import REFRESH, FakeGoogle

from vaultlines import cli, google, runtime, vaults
from vaultlines.plugins import gdrive
from vaultlines.util import fetch_dir, vaults_dir

CLIENT = "1234-abc.apps.googleusercontent.com"
SOURCE = f'''about = "The text of every file in the Mixim HQ shared drive. Claude only reads it."

[source]
kind                 = "gdrive"
shared_drive         = "Mixim HQ"
folder               = ""
max_size             = "50M"
google_client_id     = "{CLIENT}"
google_client_secret = "GOCSPX-x"
'''
CREATE = ["vault", "create", "mixim-ai/vault-hq", "--source", "gdrive", "--shared_drive", "Mixim HQ",
          "--google_client_id", CLIENT, "--google_client_secret", "GOCSPX-x"]


def note(path, file_id, mime="application/pdf"):
    meta = {"title": "x", "type": "drive-file", "source": "gdrive", "id": file_id, "path": path, "mime": mime,
            "fetch": gdrive.fetch_command("mixim-ai/vault-hq", path)}
    return gdrive.render_note(meta, [], "text\n")


@pytest.fixture
def google_fake(monkeypatch):
    g = FakeGoogle()
    monkeypatch.setenv("VAULTLINES_FAKE_GOOGLE", g.url)
    yield g
    g.close()


@pytest.fixture
def hq(fake_github, computer, google_fake):
    """mixim-ai with a vault filled from Google Drive, already filled by its fill job."""
    gh = fake_github
    gh.org("mixim-ai", ["alice", "bob"])
    gh.vault("mixim-ai/vault-public", ["alice", "bob"])
    gh.repo("mixim-ai/vault-hq", {"push": ["alice"], "read": ["bob"]},
            {"vault.toml": SOURCE, "Finance/Runway.xlsx.md": note("Finance/Runway.xlsx", "F1"),
             "Team/Plan.md": note("Team/Plan.md", "G1", "text/markdown"),
             "Legal/Secret.pdf.md": note("Legal/Secret.pdf", "F403")})
    google_fake.file("F1", "Runway.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     b"PK original bytes")
    google_fake.file("G1", "Plan", "application/vnd.google-apps.document")
    google_fake.file("F403", "Secret.pdf", "application/pdf", status=403)
    return gh


# ---------------------------------------------------------------- vl vault create --source KIND

def test_create_makes_the_vault_its_fill_job_and_secret(fake_github, computer, google_fake, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    assert vl(*CREATE) == 0
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err
    data = fake_github.load()
    assert data["repos"]["mixim-ai/vault-hq"] == {"push": ["alice"]}  # a vault with a source is always published
    assert data["secrets"] == {"mixim-ai/vault-hq": {"VL_SOURCE_TOKEN": REFRESH}}
    assert data["workflow_runs"] == [{"repo": "mixim-ai/vault-hq", "workflow": "vl-source.yml", "inputs": {}}]
    bare = fake_github.root / "mixim-ai" / "vault-hq.git"
    shown = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:vault.toml"], capture_output=True, text=True)
    info, problems = vaults.parse_vault_toml(shown.stdout)
    assert problems == []
    assert info.about == "The text of every file in the Mixim HQ shared drive. Claude only reads it."
    # Every key is exactly the flag that set it.
    assert info.source == {"kind": "gdrive", "shared_drive": "Mixim HQ", "folder": "", "max_size": "50M",
                           "google_client_id": CLIENT, "google_client_secret": "GOCSPX-x"}
    workflow = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:.github/workflows/vl-source.yml"],
                              capture_output=True, text=True).stdout
    assert workflow == cli.source_workflow(gdrive)
    assert "vl source refresh --here" in workflow and "rclone" in workflow
    assert (vaults_dir() / "mixim-ai" / "vault-hq" / "vault.toml").exists()
    assert google.load_token(CLIENT) is None  # the bot's login goes to GitHub only


def test_create_takes_every_key_of_the_kind(fake_github, computer, google_fake):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    assert vl(*CREATE, "--folder", "Finance/2024", "--max_size", "10M", "--about", "Finance.") == 0
    info = vaults.read("mixim-ai/vault-hq", vaults_dir() / "mixim-ai" / "vault-hq").info
    assert (info.about, info.source["folder"], info.source["max_size"]) == ("Finance.", "Finance/2024", "10M")


def test_help_for_a_kind_lists_only_its_keys(capsys):
    assert vl("vault", "create", "--source", "gdrive", "--help") == 0
    out = capsys.readouterr().out
    for key in ("--shared_drive", "--folder", "--max_size", "--google_client_id", "--google_client_secret"):
        assert key in out
    assert vl("vault", "create", "--help") == 0
    out = capsys.readouterr().out
    assert "--shared_drive" not in out and "--source KIND" in out and "gdrive" in out


@pytest.mark.parametrize("args, message", [
    (["--source", "nope"], "no source kind 'nope'. Kinds: gdrive"),
    (["--source", "gdrive", "--shared_drive", "Nope", "--google_client_id", CLIENT, "--google_client_secret", "s"],
     "No shared drive named 'Nope'"),
    (CREATE[3:] + ["--folder", "../x"], "folder"),
    (CREATE[3:][:4] + ["--google_client_id", CLIENT], "google_client_secret: missing"),
    (CREATE[3:] + ["--notes_from", "mixim-ai/marketing"], "a vault with a source can't take notes"),
])
def test_create_refuses(fake_github, computer, google_fake, capsys, args, message):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    code = vl("vault", "create", "mixim-ai/vault-hq", *args)
    assert code != 0
    assert message in capsys.readouterr().err
    assert "mixim-ai/vault-hq" not in fake_github.load()["repos"]


def test_create_refuses_a_bot_that_can_change_drive(fake_github, computer, google_fake, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    google_fake.scope = "https://www.googleapis.com/auth/drive"
    assert vl(*CREATE) == 1
    assert "can change Google Drive" in capsys.readouterr().err
    assert "mixim-ai/vault-hq" not in fake_github.load()["repos"]


def test_create_checks_github_can_take_a_workflow_first(fake_github, computer, google_fake, monkeypatch, capsys):
    from vaultlines import github

    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    monkeypatch.setattr(github, "scopes", lambda: {"repo", "admin:org"})
    assert vl(*CREATE) == 1
    assert "gh auth refresh -h github.com -s workflow" in capsys.readouterr().err
    assert not any(r.startswith("/auth") for r in google_fake.requests)  # stopped before the Google login


# ---------------------------------------------------------------- org join, fetch, login, refresh

def test_joining_tells_you_how_to_log_in_for_originals(hq, capsys):
    assert vl("org", "join", "mixim-ai") == 0
    assert "vl source login mixim-ai/vault-hq" in capsys.readouterr().out
    assert runtime.load()["vaults"]["mixim-ai-hq"]["source"] == "gdrive"


def test_fetch_logs_in_and_downloads_one_original(hq, capsys):
    vl("org", "join", "mixim-ai")
    capsys.readouterr()
    assert vl("source", "fetch", "mixim-ai/vault-hq", "Finance/Runway.xlsx") == 0
    out, err = capsys.readouterr()
    path = fetch_dir("mixim-ai/vault-hq") / "Finance" / "Runway.xlsx"
    assert out.strip().splitlines()[-1] == str(path)
    assert path.read_bytes() == b"PK original bytes"
    assert not os.access(path, os.W_OK)  # a read-only copy
    assert time.time() - path.stat().st_mtime < 60  # so `vl sync` keeps it for a day
    assert "SECRET" not in out + err
    assert google.load_token(CLIENT) == REFRESH
    assert vl("source", "fetch", "mixim-ai-hq", "Finance/Runway.xlsx") == 0  # by short name, again


def test_fetch_exports_google_files(hq, capsys):
    vl("org", "join", "mixim-ai")
    assert vl("source", "fetch", "mixim-ai/vault-hq", "Team/Plan.md") == 0
    path = fetch_dir("mixim-ai/vault-hq") / "Team" / "Plan.docx"
    assert capsys.readouterr().out.strip().splitlines()[-1] == str(path)
    assert path.read_bytes().startswith(b"EXPORTED application/vnd.openxmlformats-officedocument.wordprocessingml")


@pytest.mark.parametrize("path, message", [
    ("Legal/Secret.pdf", ("You can read mixim-ai-hq, but your Google account can't open this file in Drive. "
                          "Ask for access to Mixim HQ.")),
    ("Nope.pdf", "No note in mixim-ai/vault-hq has the path 'Nope.pdf'"),
    ("../etc/passwd", "isn't a path inside the drive"),
])
def test_fetch_refuses(hq, capsys, path, message):
    vl("org", "join", "mixim-ai")
    google.save_token(CLIENT, REFRESH)
    capsys.readouterr()
    assert vl("source", "fetch", "mixim-ai/vault-hq", path) == 1
    err = capsys.readouterr().err
    assert message in err and "SECRET" not in err


def test_a_deleted_file_is_no_access_too(hq, google_fake, capsys):
    vl("org", "join", "mixim-ai")
    del google_fake.files["F1"]
    assert vl("source", "fetch", "mixim-ai/vault-hq", "Finance/Runway.xlsx") == 1
    assert "can't open this file in Drive" in capsys.readouterr().err


def test_source_commands_need_a_vault_with_a_source(hq, capsys):
    vl("org", "join", "mixim-ai")
    for args in (["fetch", "mixim-ai/vault-public", "x.pdf"], ["login", "mixim-ai/vault-public"],
                 ["refresh", "mixim-ai/vault-public"]):
        assert vl("source", *args) == 1
        assert "mixim-ai/vault-public has no source" in capsys.readouterr().err


def test_fetch_refuses_a_login_that_can_change_drive(hq, google_fake, capsys):
    vl("org", "join", "mixim-ai")
    google.save_token(CLIENT, REFRESH)
    google_fake.scope = "https://www.googleapis.com/auth/drive"
    assert vl("source", "fetch", "mixim-ai/vault-hq", "Finance/Runway.xlsx") == 1
    assert "can change Google Drive" in capsys.readouterr().err
    assert not (fetch_dir("mixim-ai/vault-hq") / "Finance" / "Runway.xlsx").exists()


def test_login(hq):
    vl("org", "join", "mixim-ai")
    assert vl("source", "login", "mixim-ai/vault-hq") == 0
    assert google.load_token(CLIENT) == REFRESH


def test_refresh_starts_the_fill_job_and_force_rebuilds(hq):
    vl("org", "join", "mixim-ai")
    assert vl("source", "refresh", "mixim-ai/vault-hq") == 0
    assert vl("source", "refresh", "mixim-ai-hq", "--force") == 0
    assert hq.load()["workflow_runs"] == [
        {"repo": "mixim-ai/vault-hq", "workflow": "vl-source.yml", "inputs": {}},
        {"repo": "mixim-ai/vault-hq", "workflow": "vl-source.yml", "inputs": {"force": "true"}}]


def test_sync_only_pulls_a_vault_with_a_source(hq, capsys):
    vl("org", "join", "mixim-ai")
    path = vaults_dir() / "mixim-ai" / "vault-hq"
    (path / "Finance" / "Runway.xlsx.md").write_text("edited here\n")
    work = hq.root / ".work" / "mixim-ai" / "vault-hq"
    commit_files(work, {"Team/New.md": note("Team/New.md", "N1")}, "Update from Google Drive")
    capsys.readouterr()
    assert vl("sync") == 0
    out = capsys.readouterr().out
    assert "mixim-ai/vault-hq: synced. This vault is filled on GitHub, so local changes were moved" in out
    assert (path / "Team" / "New.md").exists()
    assert (path / "Finance" / "Runway.xlsx.md").read_text() != "edited here\n"
    assert vl("status") == 0
    assert "filled from Google Drive, updated" in capsys.readouterr().out


def test_old_fetched_files_are_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path))
    old, new = fetch_dir("o/vault-d") / "a" / "old.pdf", fetch_dir("o/vault-d") / "new.pdf"
    for p in (old, new):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        p.chmod(0o444)
    os.utime(old, (time.time() - 2 * 86400,) * 2)
    assert cli.clean_fetched() == 1
    assert not old.exists() and not old.parent.exists() and new.exists()


# ---------------------------------------------------------------- the fill job: `vl source refresh --here`

needs_tools = pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("uv")),
                                 reason="rclone or uv isn't installed")


def _checkout(fake_github, tmp_path, toml):
    """The fill job's checkout of the vault."""
    fake_github.repo("mixim-ai/vault-hq", ["alice"], {"vault.toml": toml})
    work = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", fake_github.url("mixim-ai/vault-hq"), str(work)], check=True)
    return work


@needs_tools
def test_refresh_here_converts_commits_and_pushes(fake_github, computer, tmp_path, monkeypatch, capsys):
    drive_folder = tmp_path / "drive"
    (drive_folder / "Team").mkdir(parents=True)
    (drive_folder / "Team" / "Plan.md").write_text("# Plan\n")
    # In tests, shared_drive may be a local folder standing in for the drive.
    work = _checkout(fake_github, tmp_path, SOURCE.replace('"Mixim HQ"', json.dumps(str(drive_folder))))
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_REPOSITORY", "mixim-ai/vault-hq")
    assert vl("source", "refresh", "--here") == 0
    assert "1 new" in capsys.readouterr().out
    bare = fake_github.root / "mixim-ai" / "vault-hq.git"
    log = subprocess.run(["git", "--git-dir", str(bare), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.splitlines()[0] == "Update from Google Drive"
    shown = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:Team/Plan.md"], capture_output=True, text=True)
    assert 'fetch: "vl source fetch mixim-ai/vault-hq \\"Team/Plan.md\\""' in shown.stdout
    assert vl("source", "refresh", "--here") == 0
    assert "no changes" in capsys.readouterr().out


def test_refresh_here_needs_the_token(fake_github, computer, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_checkout(fake_github, tmp_path, SOURCE))
    monkeypatch.delenv("VL_SOURCE_TOKEN", raising=False)
    assert vl("source", "refresh", "--here") == 1
    assert "VL_SOURCE_TOKEN" in capsys.readouterr().err


def test_refresh_here_refuses_a_token_that_can_change_drive(fake_github, computer, google_fake, tmp_path, monkeypatch,
                                                            capsys):
    monkeypatch.chdir(_checkout(fake_github, tmp_path, SOURCE))
    monkeypatch.setenv("VL_SOURCE_TOKEN", REFRESH)
    google_fake.scope = "https://www.googleapis.com/auth/drive"
    assert vl("source", "refresh", "--here") == 1
    err = capsys.readouterr().err
    assert "can change Google Drive" in err and "SECRET" not in err


def test_refresh_here_finds_the_shared_drive_by_name(fake_github, computer, google_fake, tmp_path, monkeypatch):
    monkeypatch.chdir(_checkout(fake_github, tmp_path, SOURCE))
    monkeypatch.setenv("VL_SOURCE_TOKEN", REFRESH)
    seen = {}

    def fake_run(root, source, vault_id, remote, conf, force=False):
        with open(conf) as f:
            seen["conf"] = f.read()
        return "0 new, 0 changed, 0 moved, 0 deleted"

    monkeypatch.setattr(gdrive, "run", fake_run)
    assert vl("source", "refresh", "--here") == 0
    assert "team_drive = 0AHF8p0HI9kM1Uk9PVA\n" in seen["conf"]
