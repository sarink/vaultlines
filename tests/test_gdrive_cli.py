"""`vl gdrive ...`: add (admins), fetch, login, rebuild, and run (the GitHub Action)."""

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
shared_drive         = "0AHF8p0HI9kM1Uk9PVA"
shared_drive_name    = "Mixim HQ"
folder               = ""
max_size             = "50M"
google_client_id     = "{CLIENT}"
google_client_secret = "GOCSPX-x"
'''


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
    """mixim-ai with a Drive vault, already filled by its Action."""
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


def test_joining_tells_you_how_to_sign_in_for_originals(hq, capsys):
    assert vl("org", "join", "mixim-ai") == 0
    out = capsys.readouterr().out
    assert "vl gdrive login mixim-ai/vault-hq" in out
    assert runtime.load()["vaults"]["mixim-ai-hq"]["source"] == "gdrive"


def test_fetch_signs_in_and_downloads_one_original(hq, capsys):
    vl("org", "join", "mixim-ai")
    capsys.readouterr()
    assert vl("gdrive", "fetch", "mixim-ai/vault-hq", "Finance/Runway.xlsx") == 0
    out, err = capsys.readouterr()
    path = fetch_dir("mixim-ai/vault-hq") / "Finance" / "Runway.xlsx"
    assert out.strip().splitlines()[-1] == str(path)
    assert path.read_bytes() == b"PK original bytes"
    assert not os.access(path, os.W_OK)  # a read-only copy
    assert time.time() - path.stat().st_mtime < 60  # so `vl sync` keeps it for a day
    assert "SECRET" not in out + err
    assert google.load_token(CLIENT) == REFRESH
    assert vl("gdrive", "fetch", "mixim-ai-hq", "Finance/Runway.xlsx") == 0  # by short name, again


def test_fetch_exports_google_files(hq, capsys):
    vl("org", "join", "mixim-ai")
    assert vl("gdrive", "fetch", "mixim-ai/vault-hq", "Team/Plan.md") == 0
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
    assert vl("gdrive", "fetch", "mixim-ai/vault-hq", path) == 1
    err = capsys.readouterr().err
    assert message in err and "SECRET" not in err


def test_a_deleted_file_is_no_access_too(hq, google_fake, capsys):
    vl("org", "join", "mixim-ai")
    del google_fake.files["F1"]
    assert vl("gdrive", "fetch", "mixim-ai/vault-hq", "Finance/Runway.xlsx") == 1
    assert "can't open this file in Drive" in capsys.readouterr().err


def test_fetch_only_works_on_drive_vaults(hq, capsys):
    vl("org", "join", "mixim-ai")
    assert vl("gdrive", "fetch", "mixim-ai/vault-public", "x.pdf") == 1
    assert "isn't filled from Google Drive" in capsys.readouterr().err


def test_fetch_refuses_a_sign_in_that_can_change_drive(hq, google_fake, capsys):
    vl("org", "join", "mixim-ai")
    google.save_token(CLIENT, REFRESH)
    google_fake.scope = "https://www.googleapis.com/auth/drive"
    assert vl("gdrive", "fetch", "mixim-ai/vault-hq", "Finance/Runway.xlsx") == 1
    assert "can change Google Drive" in capsys.readouterr().err
    assert not (fetch_dir("mixim-ai/vault-hq") / "Finance" / "Runway.xlsx").exists()


def test_login(hq):
    vl("org", "join", "mixim-ai")
    assert vl("gdrive", "login", "mixim-ai/vault-hq") == 0
    assert google.load_token(CLIENT) == REFRESH


def test_rebuild_starts_the_workflow(hq):
    vl("org", "join", "mixim-ai")
    assert vl("gdrive", "rebuild", "mixim-ai/vault-hq") == 0
    assert hq.load()["workflow_runs"] == [{"repo": "mixim-ai/vault-hq", "workflow": "vl-gdrive.yml",
                                           "inputs": {"rebuild": "true"}}]


def test_add_makes_the_vault_its_workflow_and_secret(fake_github, computer, google_fake, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    assert vl("gdrive", "add", "mixim-ai/vault-hq", "--shared-drive", "Mixim HQ", "--client-id", CLIENT,
              "--client-secret", "GOCSPX-x") == 0
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err
    data = fake_github.load()
    assert data["repos"]["mixim-ai/vault-hq"] == {"push": ["alice"]}
    assert data["secrets"] == {"mixim-ai/vault-hq": {"VL_GDRIVE_TOKEN": REFRESH}}
    assert data["workflow_runs"] == [{"repo": "mixim-ai/vault-hq", "workflow": "vl-gdrive.yml", "inputs": {}}]
    bare = fake_github.root / "mixim-ai" / "vault-hq.git"
    shown = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:vault.toml"], capture_output=True, text=True)
    info, problems = vaults.parse_vault_toml(shown.stdout)
    assert problems == []
    assert info.about == "The text of every file in the Mixim HQ shared drive. Claude only reads it."
    assert info.source == {"kind": "gdrive", "shared_drive": "0AHF8p0HI9kM1Uk9PVA", "shared_drive_name": "Mixim HQ",
                           "folder": "", "max_size": "50M", "google_client_id": CLIENT,
                           "google_client_secret": "GOCSPX-x"}
    workflow = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:.github/workflows/vl-gdrive.yml"],
                              capture_output=True, text=True).stdout
    assert workflow == gdrive.workflow()
    assert (vaults_dir() / "mixim-ai" / "vault-hq" / "vault.toml").exists()
    assert google.load_token(CLIENT) is None  # the bot's sign-in goes to GitHub only


@pytest.mark.parametrize("args, message", [
    (["--shared-drive", "Nope"], "No shared drive named 'Nope'"),
    (["--shared-drive", "Mixim HQ", "--folder", "../x"], "folder"),
])
def test_add_refuses(fake_github, computer, google_fake, capsys, args, message):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    assert vl("gdrive", "add", "mixim-ai/vault-hq", *args, "--client-id", CLIENT, "--client-secret", "s") == 1
    assert message in capsys.readouterr().err
    assert "mixim-ai/vault-hq" not in fake_github.load()["repos"]


def test_add_refuses_a_bot_that_can_change_drive(fake_github, computer, google_fake, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    google_fake.scope = "https://www.googleapis.com/auth/drive"
    assert vl("gdrive", "add", "mixim-ai/vault-hq", "--shared-drive", "Mixim HQ", "--client-id", CLIENT,
              "--client-secret", "s") == 1
    assert "can change Google Drive" in capsys.readouterr().err
    assert "mixim-ai/vault-hq" not in fake_github.load()["repos"]


def test_sync_only_pulls_a_drive_vault(hq, capsys):
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


# ---------------------------------------------------------------- the Action: `vl gdrive run`

needs_tools = pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("uv")),
                                 reason="rclone or uv isn't installed")


def _checkout(fake_github, tmp_path, drive_folder):
    """The Action's checkout of a vault whose [source] is a local folder (tests only)."""
    toml = SOURCE.replace('"0AHF8p0HI9kM1Uk9PVA"', json.dumps(str(drive_folder)))
    fake_github.repo("mixim-ai/vault-hq", ["alice"], {"vault.toml": toml})
    work = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", fake_github.url("mixim-ai/vault-hq"), str(work)], check=True)
    return work


@needs_tools
def test_run_converts_commits_and_pushes(fake_github, computer, tmp_path, monkeypatch, capsys):
    drive_folder = tmp_path / "drive"
    (drive_folder / "Team").mkdir(parents=True)
    (drive_folder / "Team" / "Plan.md").write_text("# Plan\n")
    work = _checkout(fake_github, tmp_path, drive_folder)
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_REPOSITORY", "mixim-ai/vault-hq")
    assert vl("gdrive", "run") == 0
    assert "1 new" in capsys.readouterr().out
    bare = fake_github.root / "mixim-ai" / "vault-hq.git"
    log = subprocess.run(["git", "--git-dir", str(bare), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.splitlines()[0] == "Update from Google Drive"
    shown = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:Team/Plan.md"], capture_output=True, text=True)
    assert 'fetch: "vl gdrive fetch mixim-ai/vault-hq \\"Team/Plan.md\\""' in shown.stdout
    assert vl("gdrive", "run") == 0
    assert "no changes" in capsys.readouterr().out


def test_run_needs_the_token_for_a_real_drive(fake_github, computer, tmp_path, monkeypatch, capsys):
    fake_github.repo("mixim-ai/vault-hq", ["alice"], {"vault.toml": SOURCE})
    work = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", fake_github.url("mixim-ai/vault-hq"), str(work)], check=True)
    monkeypatch.chdir(work)
    monkeypatch.delenv("VL_GDRIVE_TOKEN", raising=False)
    assert vl("gdrive", "run") == 1
    assert "VL_GDRIVE_TOKEN" in capsys.readouterr().err


def test_run_refuses_a_token_that_can_change_drive(fake_github, computer, google_fake, tmp_path, monkeypatch, capsys):
    fake_github.repo("mixim-ai/vault-hq", ["alice"], {"vault.toml": SOURCE})
    work = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", fake_github.url("mixim-ai/vault-hq"), str(work)], check=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("VL_GDRIVE_TOKEN", REFRESH)
    google_fake.scope = "https://www.googleapis.com/auth/drive"
    assert vl("gdrive", "run") == 1
    err = capsys.readouterr().err
    assert "can change Google Drive" in err and "SECRET" not in err


def test_add_checks_github_can_take_a_workflow_first(fake_github, computer, google_fake, monkeypatch, capsys):
    from vaultlines import github

    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    monkeypatch.setattr(github, "scopes", lambda: {"repo", "admin:org"})
    assert vl("gdrive", "add", "mixim-ai/vault-hq", "--shared-drive", "Mixim HQ", "--client-id", CLIENT,
              "--client-secret", "s") == 1
    assert "gh auth refresh -s workflow" in capsys.readouterr().err
    assert not any(r.startswith("/auth") for r in google_fake.requests)  # stopped before the Google sign-in
