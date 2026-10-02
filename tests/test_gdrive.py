"""The Google Drive source: the [source] table, planning changes, notes, and real runs
with a local folder standing in for Drive."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vaultlines import gitsync
from vaultlines.plugins import gdrive as drive
from vaultlines.util import VlError

FIXTURES = Path(__file__).parent / "fixtures"
needs_tools = pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("uv")),
                                 reason="rclone or uv isn't installed")
VAULT = "mixim-ai/vault-hq"
SOURCE = {"kind": "gdrive", "shared_drive": "Mixim HQ", "folder": "",
          "max_size": "50M", "google_client_id": "1234-abc.apps.googleusercontent.com",
          "google_client_secret": "GOCSPX-x"}


# ---------------------------------------------------------------- the [source] table

def test_a_good_source():
    assert drive.validate_source(SOURCE) == []
    assert drive.validate_source({**SOURCE, "folder": "Finance/2024"}) == []


@pytest.mark.parametrize("change, problem", [
    ({"shared_drive": 5}, "shared_drive"),
    ({"shared_drive": "/abs"}, "shared_drive"),
    ({"folder": "/abs"}, "folder"),
    ({"folder": "a/../b"}, "folder"),
    ({"max_size": "lots"}, "max_size"),
    ({"google_client_id": ""}, "google_client_id"),
    ({"google_client_secret": None}, "google_client_secret"),
    ({"colour": "red"}, "colour: unknown key"),
])
def test_source_problems(change, problem):
    source = {k: v for k, v in {**SOURCE, **change}.items() if v is not None}
    assert any(problem in p for p in drive.validate_source(source))


def test_a_local_folder_counts_only_in_tests(monkeypatch):
    local = {**SOURCE, "shared_drive": "/tmp/drive"}
    assert drive.validate_source(local)
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    assert drive.validate_source(local) == []


def test_the_rclone_config_holds_the_token_and_the_drive():
    text = drive.rclone_config(SOURCE, "0AHF8p0HI9kM1Uk9PVA", "ya29.ACCESS", "1//REFRESH")
    assert "[gdrive]\ntype = drive\nscope = drive.readonly\n" in text
    assert "client_id = 1234-abc.apps.googleusercontent.com\n" in text
    assert "team_drive = 0AHF8p0HI9kM1Uk9PVA\n" in text
    token = json.loads(next(line for line in text.splitlines() if line.startswith("token = "))[len("token = "):])
    assert token["access_token"] == "ya29.ACCESS" and token["refresh_token"] == "1//REFRESH"
    assert drive.remote_path({**SOURCE, "folder": "Finance/2024"}) == "gdrive:Finance/2024"
    assert drive.remote_path(SOURCE) == "gdrive:"


def test_the_fill_job_runs_this_version_of_vl_hourly():
    from vaultlines import __version__
    from vaultlines.cli import source_workflow

    text = source_workflow(drive)
    assert 'cron: "17 * * * *"' in text
    assert f'uvx --from "git+https://github.com/sarink/vaultlines@v{__version__}" vl source refresh --here' in text
    assert "VL_SOURCE_TOKEN: ${{ secrets.VL_SOURCE_TOKEN }}" in text
    assert "concurrency: { group: vl-source }" in text
    assert "rclone.org/install.sh" in text  # the kind's own setup steps


def test_rclone_runs_without_rclone_variables(monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", "/evil.conf")
    monkeypatch.setenv("RCLONE_DRIVE_SCOPE", "drive")
    monkeypatch.setenv("PATH_KEEP", "yes")
    env = drive._env()
    assert "RCLONE_CONFIG" not in env and "RCLONE_DRIVE_SCOPE" not in env
    assert env["PATH_KEEP"] == "yes"


def test_parse_size():
    assert drive.parse_size("50M") == 50 * 1024 ** 2
    assert drive.parse_size("1k") == 1024
    assert drive.parse_size("2G") == 2 * 1024 ** 3
    assert drive.parse_size("100") == 100


# ---------------------------------------------------------------- planning changes

def F(path, id="", modified="2024-01-01T00:00:00Z", md5="m1", size=10, mime="application/pdf"):
    return drive.File(id=id, path=path, size=size, mime=mime, modified=modified, md5=md5)


def N(file, path, id="", modified="2024-01-01T00:00:00Z", md5="m1", converter=drive.CONVERTER, source="gdrive"):
    meta = {"source": source, "path": path, "modified": modified, "md5": md5, "converter": converter}
    if id:
        meta["id"] = id
    return drive.Note(file=file, meta=meta, extra=[])


def kinds(plan):
    return sorted((c.kind, c.dest or c.note.file) for c in plan)


def test_note_paths():
    files = [F("Plan.md", "1"), F("Finance/Runway.xlsx", "2"), F("Contract", "3"), F("a/b.pdf.md", "4")]
    assert drive.note_paths(files) == {"1": "Plan.md", "2": "Finance/Runway.xlsx.md", "3": "Contract.md", "4": "a/b.pdf.md"}


def test_note_path_collisions_get_part_of_the_id():
    files = [F("Plan.md", "1BCDEFGHIJ"), F("Plan", "0ABCDEFGH")]  # a Doc, and a file with no extension
    assert drive.note_paths(files) == {"0ABCDEFGH": "Plan.md", "1BCDEFGHIJ": "Plan (1BCDEF).md"}


def test_files_without_ids_are_keyed_by_path():
    paths = drive.note_paths([F("Plan.md"), F("Plan")])
    assert sorted(paths) == ["path:Plan", "path:Plan.md"]
    assert paths["path:Plan"] == "Plan.md" and paths["path:Plan.md"].startswith("Plan (")


def test_changes_add_unchanged_delete():
    files = [F("new.pdf", "1"), F("same.pdf", "2")]
    notes = [N("same.pdf.md", "same.pdf", "2"), N("gone.pdf.md", "gone.pdf", "3")]
    assert kinds(drive.changes(files, notes)) == [("add", "new.pdf.md"), ("delete", "gone.pdf.md")]


@pytest.mark.parametrize("note_change", [{"modified": "2023-01-01T00:00:00Z"}, {"md5": "old"}, {"converter": "markitdown 0.0.1"}])
def test_changes_update(note_change):
    note = N("a.pdf.md", "a.pdf", "1")
    note.meta.update(note_change)
    plan = drive.changes([F("a.pdf", "1")], [note])
    assert kinds(plan) == [("update", "a.pdf.md")]
    assert plan[0].note is note


def test_force_updates_everything():
    plan = drive.changes([F("a.pdf", "1")], [N("a.pdf.md", "a.pdf", "1")], force=True)
    assert kinds(plan) == [("update", "a.pdf.md")]


def test_changes_move_and_move_with_change():
    files = [F("Archive/a.pdf", "1"), F("Archive/b.pdf", "2", md5="m2")]
    notes = [N("a.pdf.md", "a.pdf", "1"), N("b.pdf.md", "b.pdf", "2")]
    plan = {c.note.file: c for c in drive.changes(files, notes)}
    assert (plan["a.pdf.md"].kind, plan["a.pdf.md"].dest) == ("move", "Archive/a.pdf.md")
    assert (plan["b.pdf.md"].kind, plan["b.pdf.md"].dest) == ("update", "Archive/b.pdf.md")


def test_a_note_moved_by_hand_is_moved_back():
    plan = drive.changes([F("a.pdf", "1")], [N("elsewhere/a.pdf.md", "a.pdf", "1")])
    assert kinds(plan) == [("move", "a.pdf.md")]


def test_two_notes_for_one_file_keep_one():
    plan = drive.changes([F("a.pdf", "1")], [N("a.pdf.md", "a.pdf", "1"), N("copy.md", "a.pdf", "1")])
    assert kinds(plan) == [("delete", "copy.md")]


def test_other_sources_notes_are_not_read(tmp_path):
    (tmp_path / "mine.md").write_text(drive.render_note({"source": "gdrive", "path": "mine"}, [], "x"))
    (tmp_path / "theirs.md").write_text(drive.render_note({"source": "other", "path": "theirs"}, [], "x"))
    (tmp_path / "plain.md").write_text("# Just a note\n")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s.md").write_text(drive.render_note({"source": "gdrive", "path": "s"}, [], "x"))
    assert [n.file for n in drive.read_notes(tmp_path)] == ["mine.md"]


@pytest.mark.parametrize("path", [".git/config", "x/.git/hooks/pre-commit", "sessions/a.pdf", ".obsidian/x.md",
                                  ".DS_Store", "../up.pdf", "a/../b.pdf", "line\nbreak.pdf"])
def test_paths_vl_never_writes(path):
    assert drive.note_paths([F(path, "1")]) == {}


# ---------------------------------------------------------------- notes

def test_frontmatter_round_trip():
    meta = {"title": 'Runway "2025"', "type": "drive-file", "source": "gdrive", "id": "1AbC",
            "path": "Finance/Runway.xlsx", "modified": "2024-12-18T19:43:47Z", "text": "full",
            "fetch": 'vl source fetch mixim-ai/vault-hq "Finance/Runway.xlsx"'}
    text = drive.render_note(meta, [], "## Summary\n| a |\n")
    assert text.startswith('---\ntitle: "Runway \\"2025\\""\ntype: "drive-file"\n')
    parsed, extra, body = drive.parse_note(text)
    assert parsed == meta and extra == [] and body == "## Summary\n| a |\n"


def test_frontmatter_keeps_keys_basic_memory_added():
    text = ('---\ntitle: Runway\ntype: "drive-file"\nsource: gdrive\nid: "1"\npath: \'Finance/Runway.xlsx\'\n'
            'tags:\n- finance\n- cash\npermalink: finance/runway\n---\n\nbody\n')
    meta, extra, body = drive.parse_note(text)
    assert meta["title"] == "Runway" and meta["source"] == "gdrive" and meta["path"] == "Finance/Runway.xlsx"
    assert extra == ["tags:", "- finance", "- cash", "permalink: finance/runway"]
    again = drive.render_note({**meta, "path": "Archive/Runway.xlsx"}, extra, body)
    assert "tags:\n- finance\n- cash\npermalink: finance/runway\n---" in again
    assert drive.parse_note(again)[0]["path"] == "Archive/Runway.xlsx"


def test_not_a_note():
    assert drive.parse_note("# no frontmatter\n") == ({}, [], "# no frontmatter\n")


def test_kind_of():
    assert drive.kind_of("application/pdf", "Contract") == ".pdf"  # Drive knows the type without an extension
    assert drive.kind_of("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "x.xlsx") == ".xlsx"
    assert drive.kind_of("text/plain; charset=utf-8", "a.txt") == "text"
    assert drive.kind_of("text/markdown", "Plan.md") == "text"
    assert drive.kind_of("application/octet-stream", "notes.md") == "text"
    assert drive.kind_of("application/octet-stream", "deck.pptx") == ".pptx"
    assert drive.kind_of("application/zip", "a.zip") is None
    assert drive.kind_of("video/mp4", "a.mp4") is None
    assert drive.kind_of("application/octet-stream", "part.stl") is None


def test_long_text_is_cut():
    body, status = drive.cut("line\n" * 100_000, "vl source fetch o/vault-d \"a.pdf\"")
    assert status == "truncated"
    assert len(body.encode()) < drive.MAX_TEXT + 500
    assert body.rstrip().endswith('`vl source fetch o/vault-d "a.pdf"`.')
    assert drive.cut("short\n", "x") == ("short\n", "full")
    assert drive.cut("  \n\n", "x")[1] == "no text"


# ---------------------------------------------------------------- real runs, with a local folder as the "Drive"

class Vault:
    def __init__(self, path):
        self.path = path


@pytest.fixture
def local_drive(tmp_path, monkeypatch):
    uv_cache = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True).stdout.strip() if shutil.which("uv") else ""
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path / "vl"))
    if uv_cache:
        monkeypatch.setenv("UV_CACHE_DIR", uv_cache)
    src = tmp_path / "drive"
    for rel, fixture in (("Finance/Runway.xlsx", "sample.xlsx"), ("Legal/contract.docx", "sample.docx"),
                         ("deck.pptx", "sample.pptx"), ("Legal/Old/scan.pdf", "sample.pdf")):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, src / rel)
    (src / "Team Docs").mkdir()
    (src / "Team Docs" / "Plan Q3.md").write_text("# Plan\n\nWe ship in Q3.\n")
    (src / "notes.txt").write_text("plain words\n")
    (src / "blank.txt").write_text("  \n")
    (src / "archive.zip").write_bytes(b"PK\x05\x06" + bytes(18))
    (src / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really\n")
    (src / "huge.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 300_000)
    vault = tmp_path / "vault"
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    (vault / "readme.md").write_text("# By hand\n")
    source = {**SOURCE, "shared_drive": str(src), "max_size": "200K"}

    def run(force=False):
        return drive.run(vault, source, VAULT, str(src), None, force=force)

    return run, Vault(vault), src


def note(vault, rel):
    return drive.parse_note((vault.path / rel).read_text())


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


@needs_tools
def test_a_run_writes_one_note_per_file(local_drive):
    run, vault, _ = local_drive
    assert run() == "10 new, 0 changed, 0 moved, 0 deleted"
    expected = ["Finance/Runway.xlsx.md", "Legal/Old/scan.pdf.md", "Legal/contract.docx.md", "Team Docs/Plan Q3.md",
                "archive.zip.md", "blank.txt.md", "broken.pdf.md", "deck.pptx.md", "huge.pdf.md", "notes.txt.md", "readme.md"]
    assert sorted(snapshot(vault.path)) == expected

    meta, _, body = note(vault, "Finance/Runway.xlsx.md")
    assert meta["title"] == "Runway" and meta["type"] == "drive-file" and meta["source"] == "gdrive"
    assert meta["path"] == "Finance/Runway.xlsx" and meta["text"] == "full" and meta["converter"] == drive.CONVERTER
    assert meta["fetch"] == 'vl source fetch mixim-ai/vault-hq "Finance/Runway.xlsx"'
    assert meta["md5"] and meta["modified"] and "spreadsheetml" in meta["mime"]
    assert "id" not in meta and "url" not in meta  # a local folder has no Drive IDs
    assert "Comptroller" in body  # in the second tab
    assert "marmalade covenant" in note(vault, "Legal/contract.docx.md")[2]
    assert "Lighthouse roadmap" in note(vault, "deck.pptx.md")[2]
    assert "Zephyrine" in note(vault, "Legal/Old/scan.pdf.md")[2]
    assert note(vault, "Team Docs/Plan Q3.md")[2] == "# Plan\n\nWe ship in Q3.\n"
    assert note(vault, "notes.txt.md")[2] == "plain words\n"

    for rel, status in (("archive.zip.md", "not convertible"), ("huge.pdf.md", "too big"), ("blank.txt.md", "no text")):
        meta, _, body = note(vault, rel)
        assert meta["text"] == status, rel
        assert body.count("\n") == 1 and "vl source fetch mixim-ai/vault-hq" in body
    assert note(vault, "broken.pdf.md")[0]["text"].startswith("failed: ")
    assert (vault.path / "readme.md").read_text() == "# By hand\n"


@needs_tools
def test_a_second_run_changes_nothing_then_follows_drive(local_drive):
    run, vault, src = local_drive
    run()
    before = snapshot(vault.path)
    assert run() == "0 new, 0 changed, 0 moved, 0 deleted"
    assert snapshot(vault.path) == before

    (src / "notes.txt").write_text("new words\n")
    (src / "Legal" / "Old" / "scan.pdf").unlink()
    assert run() == "0 new, 1 changed, 0 moved, 1 deleted"
    assert note(vault, "notes.txt.md")[2] == "new words\n"
    assert not (vault.path / "Legal" / "Old").exists()  # empty folders go too
    assert (vault.path / "Legal" / "contract.docx.md").exists()


@needs_tools
def test_force_converts_again_and_keeps_basic_memorys_keys(local_drive):
    run, vault, _ = local_drive
    run()
    path = vault.path / "notes.txt.md"
    path.write_text(path.read_text().replace("---\n\n", "tags:\n- misc\n---\n\n", 1).replace("plain words", "edited"))
    assert run(force=True) == "0 new, 10 changed, 0 moved, 0 deleted"
    _, extra, body = note(vault, "notes.txt.md")
    assert body == "plain words\n" and extra == ["tags:", "- misc"]


@needs_tools
def test_a_move_keeps_the_body_and_basic_memorys_keys(local_drive, monkeypatch):
    run, vault, _ = local_drive
    listing = [F("Finance/Runway.xlsx", "1RUNWAY", mime="text/plain")]
    monkeypatch.setattr(drive, "_list", lambda remote, conf: listing)
    monkeypatch.setattr(drive, "_download", lambda *a: pytest.fail("a move downloads nothing"))
    meta = {"title": "Runway", "type": "drive-file", "source": "gdrive", "id": "1RUNWAY",
            "path": "Finance/Runway.xlsx", "modified": listing[0].modified, "md5": "m1", "converter": drive.CONVERTER}
    (vault.path / "Finance").mkdir()
    (vault.path / "Finance" / "Runway.xlsx.md").write_text(drive.render_note(meta, ["tags:", "- cash"], "edited by BM\n"))
    listing[0] = F("Archive/2024 Runway.xlsx", "1RUNWAY", mime="text/plain")
    assert run() == "0 new, 0 changed, 1 moved, 0 deleted"
    assert not (vault.path / "Finance").exists()
    meta, extra, body = note(vault, "Archive/2024 Runway.xlsx.md")
    assert meta["path"] == "Archive/2024 Runway.xlsx" and meta["title"] == "2024 Runway"
    assert meta["fetch"] == 'vl source fetch mixim-ai/vault-hq "Archive/2024 Runway.xlsx"'
    assert meta["url"] == "https://drive.google.com/open?id=1RUNWAY"
    assert extra == ["tags:", "- cash"] and body == "edited by BM\n"


@needs_tools
def test_a_missing_drive_fails_and_touches_nothing(local_drive):
    run, vault, src = local_drive
    shutil.rmtree(src)
    with pytest.raises(VlError, match="rclone"):
        run()
    assert sorted(snapshot(vault.path)) == ["readme.md"]


@needs_tools
def test_two_files_that_swap_names_keep_their_notes(local_drive, monkeypatch):
    run, vault, _ = local_drive
    listing = [F("a.pdf", "1AAAAA", mime="text/plain"), F("b.pdf", "2BBBBB", mime="text/plain")]
    monkeypatch.setattr(drive, "_list", lambda remote, conf: listing)
    for f, body in zip(listing, ("about A\n", "about B\n")):
        meta = {"source": "gdrive", "id": f.id, "path": f.path, "modified": f.modified, "md5": f.md5,
                "converter": drive.CONVERTER, "text": "full"}
        (vault.path / f"{f.path}.md").write_text(drive.render_note(meta, [], body))
    listing[:] = [F("b.pdf", "1AAAAA", mime="text/plain"), F("a.pdf", "2BBBBB", mime="text/plain")]
    assert run() == "0 new, 0 changed, 2 moved, 0 deleted"
    assert note(vault, "b.pdf.md")[0]["id"] == "1AAAAA" and note(vault, "b.pdf.md")[2] == "about A\n"
    assert note(vault, "a.pdf.md")[0]["id"] == "2BBBBB" and note(vault, "a.pdf.md")[2] == "about B\n"



def test_commit_only_when_something_changed(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True)
    (tmp_path / "a.md").write_text("a\n")
    assert gitsync.commit(tmp_path, "Update from Google Drive") is True
    assert gitsync.commit(tmp_path, "Update from Google Drive") is False
    assert gitsync.git(tmp_path, "rev-list", "--count", "HEAD").stdout.strip() == "1"


# ---------------------------------------------------------------- syncing a vault with a source: pull only

def _clone_pair(tmp_path):
    from conftest import push_repo

    push_repo(tmp_path / "remotes", "o/vault-d", {"a.md": "from drive\n"})
    mine = tmp_path / "mine"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remotes" / "o" / "vault-d.git"), str(mine)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@t")):
        subprocess.run(["git", "-C", str(mine), "config", key, value], check=True)
    return mine, tmp_path / "remotes" / ".work" / "o" / "vault-d"


def test_a_filled_vault_takes_the_remote(tmp_path):
    from conftest import commit_files

    mine, work = _clone_pair(tmp_path)
    commit_files(work, {"a.md": "newer\n"}, "Update from Google Drive")
    assert gitsync.pull_keeping_changes(mine) == "synced"
    assert (mine / "a.md").read_text() == "newer\n"


def test_local_changes_to_a_filled_vault_go_to_a_branch(tmp_path):
    from conftest import commit_files

    mine, work = _clone_pair(tmp_path)
    (mine / "a.md").write_text("edited here\n")
    (mine / "new.md").write_text("added here\n")
    (mine / "sessions").mkdir()
    (mine / "sessions" / "s.md").write_text("checkpoint\n")  # kept out of git, and kept
    commit_files(work, {"a.md": "newer\n"}, "Update from Google Drive")
    status = gitsync.pull_keeping_changes(mine)
    assert "local changes were moved to the branch local-changes-" in status
    assert (mine / "a.md").read_text() == "newer\n" and not (mine / "new.md").exists()
    assert (mine / "sessions" / "s.md").exists()
    branch = status.rsplit(" ", 1)[-1]
    shown = subprocess.run(["git", "-C", str(mine), "show", f"{branch}:a.md"], capture_output=True, text=True)
    assert shown.stdout == "edited here\n"
