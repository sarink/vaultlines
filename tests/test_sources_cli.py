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
from vaultlines.util import fetch_dir, refresh_dir, vaults_dir

CLIENT = "1234-abc.apps.googleusercontent.com"
HQ = "0AHF8p0HI9kM1Uk9PVA"  # the shared drive Mixim HQ, in the fake Google
SOURCE = f'''about = "The text of every file in Mixim HQ, in Google Drive. Claude only reads it."

[source]
kind                 = "gdrive"
folder_id            = "{HQ}"
max_size             = "50M"
google_client_id     = "{CLIENT}"
google_client_secret = "GOCSPX-x"
'''
CREATE = ["vault", "create", "mixim-ai/vault-hq", "--source", "gdrive",
          "--folder_id", f"https://drive.google.com/drive/folders/{HQ}?usp=sharing",
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
    """mixim-ai with a vault from Google Drive, already refreshed by its refresh job."""
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
    assert info.about == "The text of every file in Mixim HQ, in Google Drive. Claude only reads it."
    # Every key is the flag that set it; a folder's URL becomes its ID.
    assert info.source == {"kind": "gdrive", "folder_id": HQ, "max_size": "50M",
                           "google_client_id": CLIENT, "google_client_secret": "GOCSPX-x"}
    assert f'folder_id            = "{HQ}"   # Mixim HQ\n' in shown.stdout
    workflow = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:.github/workflows/vl-source.yml"],
                              capture_output=True, text=True).stdout
    assert workflow == cli.source_workflow(gdrive)
    assert "vl source refresh --fetch-only" in workflow and "rclone" in workflow
    assert "gh workflow run vl-source.yml --repo mixim-ai/vault-hq" in out  # to start it again by hand
    assert (vaults_dir() / "mixim-ai" / "vault-hq" / "vault.toml").exists()
    assert google.load_token(CLIENT) is None  # the bot's login goes to GitHub only


def test_create_takes_every_key_of_the_kind(fake_github, computer, google_fake):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    google_fake.folder("FIN", "Finance", parent=HQ, drive=HQ)
    assert vl(*CREATE, "--folder_id", "FIN", "--max_size", "10M", "--about", "Finance.") == 0
    info = vaults.read("mixim-ai/vault-hq", vaults_dir() / "mixim-ai" / "vault-hq").info
    assert (info.about, info.source["folder_id"], info.source["max_size"]) == ("Finance.", "FIN", "10M")
    assert '"FIN"   # Finance, in Mixim HQ' in (vaults_dir() / "mixim-ai" / "vault-hq" / "vault.toml").read_text()


def test_help_for_a_kind_lists_only_its_keys(capsys):
    assert vl("vault", "create", "--source", "gdrive", "--help") == 0
    out = capsys.readouterr().out
    for key in ("--folder_id", "--max_size", "--google_client_id", "--google_client_secret"):
        assert key in out
    assert "--shared_drive" not in out and "--folder " not in out
    assert vl("vault", "create", "--help") == 0
    out = capsys.readouterr().out
    assert "--folder_id" not in out and "--source KIND" in out and "gdrive" in out


def test_help_for_a_kind_says_how_to_get_what_it_needs(capsys):
    assert vl("vault", "create", "--source", "gdrive", "--help") == 0
    out = capsys.readouterr().out
    assert "console.cloud.google.com/auth/clients" in out and "Desktop app" in out and "bot account" in out


@pytest.fixture
def answers(monkeypatch):
    """A terminal that answers vl's questions in turn; `asked` gets each question."""
    from vaultlines import util

    given, asked = [], []

    def fake_input(question=""):
        asked.append(question)
        if not given:
            raise AssertionError(f"vl asked one question too many: {question!r}")
        return given.pop(0)

    monkeypatch.setattr(util, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)
    return given, asked


def test_create_without_a_terminal_says_what_is_missing_and_how_to_get_it(fake_github, computer, google_fake,
                                                                          monkeypatch, capsys):
    from vaultlines import util

    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    monkeypatch.setattr(util, "interactive", lambda: False)
    assert vl("vault", "create", "mixim-ai/vault-hq", "--source", "gdrive") == 1
    err = capsys.readouterr().err
    assert "--folder_id" in err and "--google_client_id" in err and "--google_client_secret" in err
    assert "console.cloud.google.com/auth/clients" in err  # how to get them
    assert google_fake.requests == [] and "mixim-ai/vault-hq" not in fake_github.load()["repos"]


def test_create_asks_for_what_is_missing(fake_github, computer, google_fake, answers, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    given, asked = answers
    given += ["vault-hq", "mixim-ai/vault-hq", CLIENT, "GOCSPX-x", "1"]  # a bad name first; then the first drive
    assert vl("vault", "create", "--source", "gdrive") == 0
    out = capsys.readouterr().out
    assert "console.cloud.google.com/auth/clients" in out  # the steps, before the questions
    assert "OWNER/vault-NAME" in asked[0] and "OWNER/vault-NAME" in asked[1] and "OWNER/vault-NAME" in out
    assert "google_client_id" in asked[2] and "google_client_secret" in asked[3] and "folder" in asked[4]
    assert "1. Mixim HQ (shared drive)" in out and "2. Other (shared drive)" in out and "3. My Drive" in out
    info = vaults.read("mixim-ai/vault-hq", vaults_dir() / "mixim-ai" / "vault-hq").info
    assert info.source["folder_id"] == HQ  # it has no folders, so there's nothing more to ask
    assert (info.source["google_client_id"], info.source["google_client_secret"]) == (CLIENT, "GOCSPX-x")


@pytest.mark.parametrize("args", [["--source", "gdrive"], []])
def test_create_without_a_vault_or_a_terminal_says_to_give_one(fake_github, computer, monkeypatch, capsys, args):
    from vaultlines import util

    monkeypatch.setattr(util, "interactive", lambda: False)
    assert vl("vault", "create", *args) == 1
    assert "Give the vault to create: OWNER/vault-NAME" in capsys.readouterr().err


def test_create_asks_again_for_a_bad_answer(fake_github, computer, google_fake, answers, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    given, asked = answers
    given += ["", CLIENT, "9", "Other (shared drive)"]
    assert vl("vault", "create", "mixim-ai/vault-hq", "--source", "gdrive", "--google_client_secret", "s") == 0
    assert len(asked) == 4
    info = vaults.read("mixim-ai/vault-hq", vaults_dir() / "mixim-ai" / "vault-hq").info
    assert info.source["folder_id"] == "0BOTHER"


def test_create_walks_the_folders_and_can_go_back(fake_github, computer, google_fake, answers, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    google_fake.folder("FIN", "Finance", parent=HQ, drive=HQ)
    google_fake.folder("F24", "2024", parent="FIN", drive=HQ)
    google_fake.folder("LEGAL", "Legal", parent=HQ, drive=HQ)
    google_fake.folder("BOARD", "Board decks", shared=True)
    given, _ = answers
    # Mixim HQ, Finance/, back up, Finance/ again, then all of it.
    given += ["1", "2", "(back)", "Finance/", "1"]
    assert vl("vault", "create", "mixim-ai/vault-hq", "--source", "gdrive", "--google_client_id", CLIENT,
              "--google_client_secret", "s") == 0
    out = capsys.readouterr().out
    assert "3. Board decks (folder shared with you)" in out and "4. My Drive" in out
    assert "1. All of Mixim HQ" in out and "2. Finance/" in out and "3. Legal/" in out
    assert "1. All of Mixim HQ/Finance" in out and "2. 2024/" in out and "3. (back)" in out
    path = vaults_dir() / "mixim-ai" / "vault-hq"
    info = vaults.read("mixim-ai/vault-hq", path).info
    assert info.source["folder_id"] == "FIN"
    assert info.about == "The text of every file in Mixim HQ/Finance, in Google Drive. Claude only reads it."
    assert '"FIN"   # Mixim HQ/Finance' in (path / "vault.toml").read_text()


def test_create_asks_nothing_when_every_key_is_given(fake_github, computer, google_fake, answers, capsys):
    fake_github.org("mixim-ai", ["alice"])
    vl("org", "join", "mixim-ai")
    assert vl(*CREATE) == 0
    assert answers[1] == [] and "console.cloud.google.com" not in capsys.readouterr().out


@pytest.mark.parametrize("args, message", [
    (["--source", "nope"], "no source kind 'nope'. Kinds: gdrive"),
    (["--source", "gdrive", "--folder_id", "1NOPE", "--google_client_id", CLIENT, "--google_client_secret", "s"],
     "This Google account can't open the folder 1NOPE"),
    (CREATE[3:] + ["--folder_id", "Mixim HQ"], "folder_id: should be a Drive folder's URL or ID"),
    (CREATE[3:][:4] + ["--google_client_id", CLIENT], "Missing --google_client_secret"),
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
                          f"Ask for access to it: https://drive.google.com/drive/folders/{HQ}")),
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


def test_sync_only_pulls_a_vault_with_a_source(hq, capsys):
    vl("org", "join", "mixim-ai")
    path = vaults_dir() / "mixim-ai" / "vault-hq"
    (path / "Finance" / "Runway.xlsx.md").write_text("edited here\n")
    work = hq.root / ".work" / "mixim-ai" / "vault-hq"
    commit_files(work, {"Team/New.md": note("Team/New.md", "N1")}, "Update from Google Drive")
    capsys.readouterr()
    assert vl("sync") == 0
    out = capsys.readouterr().out
    assert "mixim-ai/vault-hq: synced. This vault is refreshed from its source, so local changes were moved" in out
    assert (path / "Team" / "New.md").exists()
    assert (path / "Finance" / "Runway.xlsx.md").read_text() != "edited here\n"
    assert vl("status") == 0
    assert "from Google Drive, refreshed" in capsys.readouterr().out


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


# ---------------------------------------------------------------- vl source refresh [VAULT], on any computer

needs_tools = pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("uv")),
                                 reason="rclone or uv isn't installed")


def _checkout(fake_github, tmp_path, toml, files=None):
    """A clone of the vault, like the refresh job's checkout."""
    fake_github.repo("mixim-ai/vault-hq", ["alice"], {"vault.toml": toml, **(files or {})})
    work = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", fake_github.url("mixim-ai/vault-hq"), str(work)], check=True)
    return work


def _bare_log(fake_github):
    bare = fake_github.root / "mixim-ai" / "vault-hq.git"
    return subprocess.run(["git", "--git-dir", str(bare), "log", "--format=%s"], capture_output=True,
                          text=True).stdout.splitlines()


def _local_drive(tmp_path):
    drive_folder = tmp_path / "drive"
    (drive_folder / "Team").mkdir(parents=True)
    (drive_folder / "Team" / "Plan.md").write_text("# Plan\n")
    # In tests, folder_id may be a local folder standing in for the drive.
    return SOURCE.replace(f'"{HQ}"', json.dumps(str(drive_folder)))


@needs_tools
def test_refresh_in_the_vault_fetches_converts_commits_and_pushes(fake_github, computer, tmp_path, monkeypatch,
                                                                   capsys):
    work = _checkout(fake_github, tmp_path, _local_drive(tmp_path), {"Inbox/by hand.md": "kept\n"})
    monkeypatch.chdir(work / "Inbox")  # anywhere in the clone
    assert vl("source", "refresh") == 0
    assert "1 new" in capsys.readouterr().out
    assert _bare_log(fake_github)[0] == "Update from Google Drive"
    bare = fake_github.root / "mixim-ai" / "vault-hq.git"
    shown = subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:Team/Plan.md"], capture_output=True, text=True)
    assert 'fetch: "vl source fetch mixim-ai/vault-hq \\"Team/Plan.md\\""' in shown.stdout
    assert not refresh_dir("mixim-ai/vault-hq").exists()  # nothing is left behind
    assert vl("source", "refresh") == 0
    assert "no changes" in capsys.readouterr().out


@needs_tools
def test_refresh_in_two_steps_and_pull_before_push(fake_github, computer, tmp_path, monkeypatch, capsys):
    """The refresh job's two steps: only the fetch has the login; the convert commits and pushes."""
    monkeypatch.chdir(_checkout(fake_github, tmp_path, _local_drive(tmp_path)))
    assert vl("source", "refresh", "--fetch-only") == 0
    out = capsys.readouterr().out
    assert "Fetched 1 file" in out and "vl source refresh --convert-only" in out
    assert (refresh_dir("mixim-ai/vault-hq")).is_dir()
    assert _bare_log(fake_github) == ["seed"]  # nothing committed yet
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", fake_github.url("mixim-ai/vault-hq"), str(other)], check=True)
    commit_files(other, {"Other.md": "by someone else\n"}, "Another refresh")
    assert vl("source", "refresh", "--convert-only") == 0
    assert "1 new" in capsys.readouterr().out
    assert _bare_log(fake_github)[:2] == ["Update from Google Drive", "Another refresh"]
    assert not refresh_dir("mixim-ai/vault-hq").exists()


def test_convert_only_needs_a_fetch_first(fake_github, computer, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_checkout(fake_github, tmp_path, SOURCE))
    assert vl("source", "refresh", "--convert-only") == 1
    assert "Nothing fetched for mixim-ai/vault-hq. Run `vl source refresh --fetch-only` first." in capsys.readouterr().err
    assert vl("source", "refresh", "--convert-only", "--force") == 1
    assert "--force goes with the fetch" in capsys.readouterr().err


def test_refresh_outside_a_vault_says_to_give_one(fake_github, computer, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert vl("source", "refresh") == 1
    assert "Give the vault to refresh, like `vl source refresh acme/vault-hq`" in capsys.readouterr().err


@pytest.fixture
def fetched(monkeypatch):
    """Stand in for the fetch from Drive: keep the rclone.conf it was given, and where it was."""
    seen = {}

    def fake_fetch(root, source, remote, conf, force, staged):
        with open(conf) as f:
            seen["conf"], seen["path"] = f.read(), conf
        return "Fetched 0 files"

    monkeypatch.setattr(gdrive, "_fetch", fake_fetch)
    return seen


def test_refresh_without_a_login_says_how_to_get_one(fake_github, computer, google_fake, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_checkout(fake_github, tmp_path, SOURCE))
    monkeypatch.delenv("VL_SOURCE_TOKEN", raising=False)
    assert vl("source", "refresh", "--fetch-only") == 1
    err = capsys.readouterr().err
    assert "set VL_SOURCE_TOKEN, or run `vl source login mixim-ai/vault-hq`" in err


def test_refresh_a_vault_by_name_with_your_own_login(hq, monkeypatch, fetched, capsys):
    vl("org", "join", "mixim-ai")
    monkeypatch.delenv("VL_SOURCE_TOKEN", raising=False)
    google.save_token(CLIENT, REFRESH)
    assert vl("source", "refresh", "mixim-ai/vault-hq", "--fetch-only") == 0
    assert "team_drive = 0AHF8p0HI9kM1Uk9PVA\n" in fetched["conf"]  # found by its name
    assert not os.path.exists(fetched["path"])  # the login's file is gone after the fetch
    assert "SECRET" not in "".join(capsys.readouterr())


def test_the_token_in_the_environment_comes_first(fake_github, computer, google_fake, tmp_path, monkeypatch, fetched):
    monkeypatch.chdir(_checkout(fake_github, tmp_path, SOURCE))
    google.save_token(CLIENT, "1//SOMEONE-ELSE")  # the fake Google refuses this one
    monkeypatch.setenv("VL_SOURCE_TOKEN", REFRESH)
    assert vl("source", "refresh", "--fetch-only") == 0
    assert REFRESH in fetched["conf"]


def test_refresh_refuses_a_login_that_can_change_drive(fake_github, computer, google_fake, tmp_path, monkeypatch,
                                                      capsys):
    monkeypatch.chdir(_checkout(fake_github, tmp_path, SOURCE))
    monkeypatch.setenv("VL_SOURCE_TOKEN", REFRESH)
    google_fake.scope = "https://www.googleapis.com/auth/drive"
    assert vl("source", "refresh", "--fetch-only") == 1
    err = capsys.readouterr().err
    assert "can change Google Drive" in err and "SECRET" not in err


def test_no_fill_words_left(capsys):
    """One word for it: a vault with a source is refreshed, by its refresh job."""
    assert vl("vault", "create", "--source", "gdrive", "--help") == 0
    assert vl("source", "refresh", "--help") == 0
    assert vl("source", "--help") == 0
    out = capsys.readouterr().out.lower()
    assert "fill" not in out
    assert "fill" not in cli.source_workflow(gdrive).lower()
