"""The Google Drive source: remotes, the token check, planning changes, notes, and real runs
with a local folder standing in for Drive."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import urllib.error
from pathlib import Path

import pytest

from vaultlines.config import Config, Vault
from vaultlines.plugins import drive
from vaultlines.util import VlError

FIXTURES = Path(__file__).parent / "fixtures"
needs_tools = pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("uv")),
                                 reason="rclone or uv isn't installed")


# ---------------------------------------------------------------- remotes

@pytest.mark.parametrize("text, name, overrides, path", [
    ("mixim:", "mixim", {}, ""),
    ("vl-mixim-drive:Finance/2024", "vl-mixim-drive", {}, "Finance/2024"),
    ("mixim,team_drive=0AP0RZ-W4oPwkUk9PVA:", "mixim", {"team_drive": "0AP0RZ-W4oPwkUk9PVA"}, ""),
    ("mixim,team_drive=0AB,root_folder_id=:Legal", "mixim", {"team_drive": "0AB", "root_folder_id": ""}, "Legal"),
])
def test_parse_remote(text, name, overrides, path):
    r = drive.parse_remote(text)
    assert (r.name, r.overrides, r.path) == (name, overrides, path)


@pytest.mark.parametrize("text, problem", [
    ("mixim,service_account_file=/tmp/key.json:", "service_account_file"),
    ("mixim,token=abc:", "token"),
    ("mixim,scope=drive:", "scope"),
    ("mixim,client_id=x:", "client_id"),
    ("mixim,team_drive='0AB':", "team_drive"),
    (":drive,scope=drive:", "remote name"),
    ("mixim", "remote name"),
    ("/Users/me/Drive", "remote name"),
    ("", "remote name"),
])
def test_parse_remote_refuses(text, problem):
    with pytest.raises(VlError, match=problem):
        drive.parse_remote(text)


def test_local_paths_only_in_tests(monkeypatch):
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    r = drive.parse_remote("/tmp/drive")
    assert (r.name, r.path) == (None, "/tmp/drive")


@pytest.mark.parametrize("settings, problem", [
    ({}, "remote"),
    ({"remote": ""}, "remote"),
    ({"remote": 5}, "remote"),
    ({"remote": "mixim,token=x:"}, "remote"),
    ({"remote": "mixim:", "max_size": ""}, "max_size"),
    ({"remote": "mixim:", "max_size": 50}, "max_size"),
    ({"remote": "mixim:", "max_size": "lots"}, "max_size"),
])
def test_validate_errors(settings, problem):
    assert drive.validate(settings)[0] == problem


def test_validate_good():
    assert drive.validate({"remote": "mixim,team_drive=0AB:Legal", "max_size": "1G"}) is None


def test_parse_size():
    assert drive.parse_size("50M") == 50 * 1024 ** 2
    assert drive.parse_size("1k") == 1024
    assert drive.parse_size("2G") == 2 * 1024 ** 3
    assert drive.parse_size("100") == 100


# ---------------------------------------------------------------- the token check

READONLY = "https://www.googleapis.com/auth/drive.readonly"


def _token(access="ya29.SECRET-TOKEN", minutes=30):
    expiry = time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(time.time() + minutes * 60))
    return json.dumps({"access_token": access, "token_type": "Bearer", "refresh_token": "1//REFRESH", "expiry": expiry})


class FakeGoogle:
    """Stands in for `rclone config dump`, rclone's token refresh and Google's tokeninfo."""

    def __init__(self, monkeypatch, remote: dict, scope: str = READONLY, refreshed: dict | None = None):
        self.dumps = [{"mixim": remote}] + ([{"mixim": refreshed}] if refreshed else [])
        self.scope = scope
        self.refreshes: list[str] = []
        self.asked: list[str] = []
        self.status = 200
        self.offline = False
        monkeypatch.setattr(drive, "_config_dump", self.dump)
        monkeypatch.setattr(drive, "_refresh", self.refreshes.append)
        monkeypatch.setattr("urllib.request.urlopen", self.urlopen)

    def dump(self):
        return self.dumps.pop(0) if len(self.dumps) > 1 else self.dumps[0]

    def urlopen(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        assert url.startswith("https://oauth2.googleapis.com/tokeninfo")
        self.asked.append(url)
        if self.offline:
            raise urllib.error.URLError("nodename nor servname provided")
        if self.status != 200:
            raise urllib.error.HTTPError(url, self.status, "Bad Request", {}, io.BytesIO(b'{"error": "invalid_token"}'))
        return io.BytesIO(json.dumps({"scope": self.scope, "expires_in": 3000}).encode())


def _drive_remote(**extra):
    return {"type": "drive", "scope": "drive.readonly", "token": _token(), **extra}


def test_read_only_token_passes(monkeypatch):
    google = FakeGoogle(monkeypatch, _drive_remote())
    drive.check_read_only({"remote": "mixim,team_drive=0AB:Legal"})
    assert len(google.asked) == 1 and "ya29.SECRET-TOKEN" in google.asked[0]
    assert google.refreshes == []


def test_metadata_scope_is_read_only_too(monkeypatch):
    FakeGoogle(monkeypatch, _drive_remote(), scope=f"{READONLY} https://www.googleapis.com/auth/drive.metadata.readonly")
    drive.check_read_only({"remote": "mixim:"})


def _refused(settings, match):
    with pytest.raises(VlError, match=match) as e:
        drive.check_read_only(settings)
    assert "SECRET" not in str(e.value) and "REFRESH" not in str(e.value)
    return str(e.value)


def test_a_token_that_can_write_is_refused(monkeypatch):
    FakeGoogle(monkeypatch, _drive_remote(), scope="https://www.googleapis.com/auth/drive")
    message = _refused({"remote": "mixim:"}, "can change your Drive")
    assert "rclone remote 'mixim'" in message
    assert "scope: https://www.googleapis.com/auth/drive" in message
    assert "vl source add" in message


def test_the_scope_google_reports_wins_over_the_config(monkeypatch):
    FakeGoogle(monkeypatch, _drive_remote(scope="drive.readonly"), scope=f"{READONLY} https://www.googleapis.com/auth/drive.file")
    _refused({"remote": "mixim:"}, "can change your Drive")


@pytest.mark.parametrize("remote, match", [
    ({"type": "alias", "remote": "mixim2,team_drive=0AB:"}, "isn't a Google Drive remote"),
    ({"type": "crypt", "remote": "mixim2:"}, "isn't a Google Drive remote"),
    (_drive_remote(service_account_file="/k.json"), "service account"),
    (_drive_remote(service_account_credentials="{}"), "service account"),
    ({"type": "drive", "scope": "drive.readonly"}, "isn't signed in"),
])
def test_remotes_vl_cant_check_are_refused(monkeypatch, remote, match):
    FakeGoogle(monkeypatch, remote)
    _refused({"remote": "mixim:"}, match)


def test_an_unknown_remote_is_refused(monkeypatch):
    FakeGoogle(monkeypatch, _drive_remote())
    _refused({"remote": "other:"}, "rclone has no remote named 'other'")


def test_a_bad_override_is_refused_before_asking_rclone(monkeypatch):
    google = FakeGoogle(monkeypatch, _drive_remote())
    _refused({"remote": "mixim,service_account_file=/k.json:"}, "service_account_file")
    assert google.asked == []


def test_no_network_means_no_run(monkeypatch):
    google = FakeGoogle(monkeypatch, _drive_remote())
    google.offline = True
    _refused({"remote": "mixim:"}, "Couldn't reach Google")


def test_google_rejecting_the_token_is_refused(monkeypatch):
    google = FakeGoogle(monkeypatch, _drive_remote())
    google.status = 400
    _refused({"remote": "mixim:"}, "Google didn't accept")


def test_an_expired_token_is_refreshed_by_rclone_first(monkeypatch):
    old = _drive_remote(token=_token("ya29.OLD-SECRET", minutes=-5))
    google = FakeGoogle(monkeypatch, old, refreshed=_drive_remote(token=_token("ya29.NEW-SECRET")))
    drive.check_read_only({"remote": "mixim,team_drive=0AB:"})
    assert google.refreshes == ["mixim"]
    assert "ya29.NEW-SECRET" in google.asked[0]


def test_test_remotes_skip_the_check(monkeypatch):
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    monkeypatch.setattr(drive, "_config_dump", lambda: pytest.fail("no rclone call expected"))
    drive.check_read_only({"remote": "/tmp/drive"})


def test_rclone_runs_without_rclone_variables(monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", "/evil.conf")
    monkeypatch.setenv("RCLONE_DRIVE_SCOPE", "drive")
    monkeypatch.setenv("PATH_KEEP", "yes")
    env = drive._env()
    assert "RCLONE_CONFIG" not in env and "RCLONE_DRIVE_SCOPE" not in env
    assert env["PATH_KEEP"] == "yes"


# ---------------------------------------------------------------- planning changes

def F(path, id="", modified="2024-01-01T00:00:00Z", md5="m1", size=10, mime="application/pdf"):
    return drive.File(id=id, path=path, size=size, mime=mime, modified=modified, md5=md5)


def N(file, path, id="", modified="2024-01-01T00:00:00Z", md5="m1", converter=drive.CONVERTER, source="mixim-drive"):
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


def test_rebuild_updates_everything():
    plan = drive.changes([F("a.pdf", "1")], [N("a.pdf.md", "a.pdf", "1")], rebuild=True)
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
    (tmp_path / "mine.md").write_text(drive.render_note({"source": "mixim-drive", "path": "mine"}, [], "x"))
    (tmp_path / "theirs.md").write_text(drive.render_note({"source": "other", "path": "theirs"}, [], "x"))
    (tmp_path / "plain.md").write_text("# Just a note\n")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s.md").write_text(drive.render_note({"source": "mixim-drive", "path": "s"}, [], "x"))
    assert [n.file for n in drive.read_notes(tmp_path, "mixim-drive")] == ["mine.md"]


@pytest.mark.parametrize("path", [".git/config", "x/.git/hooks/pre-commit", "sessions/a.pdf", ".obsidian/x.md",
                                  ".DS_Store", "../up.pdf", "a/../b.pdf", "line\nbreak.pdf"])
def test_paths_vl_never_writes(path):
    assert drive.note_paths([F(path, "1")]) == {}


# ---------------------------------------------------------------- notes

def test_frontmatter_round_trip():
    meta = {"title": 'Runway "2025"', "type": "drive-file", "source": "mixim-drive", "id": "1AbC",
            "path": "Finance/Runway.xlsx", "modified": "2024-12-18T19:43:47Z", "text": "full",
            "fetch": 'vl fetch mixim-drive "Finance/Runway.xlsx"'}
    text = drive.render_note(meta, [], "## Summary\n| a |\n")
    assert text.startswith('---\ntitle: "Runway \\"2025\\""\ntype: "drive-file"\n')
    parsed, extra, body = drive.parse_note(text)
    assert parsed == meta and extra == [] and body == "## Summary\n| a |\n"


def test_frontmatter_keeps_keys_basic_memory_added():
    text = ('---\ntitle: Runway\ntype: "drive-file"\nsource: mixim-drive\nid: "1"\npath: \'Finance/Runway.xlsx\'\n'
            'tags:\n- finance\n- cash\npermalink: finance/runway\n---\n\nbody\n')
    meta, extra, body = drive.parse_note(text)
    assert meta["title"] == "Runway" and meta["source"] == "mixim-drive" and meta["path"] == "Finance/Runway.xlsx"
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
    body, status = drive.cut("line\n" * 100_000, "vl fetch d \"a.pdf\"")
    assert status == "truncated"
    assert len(body.encode()) < drive.MAX_TEXT + 500
    assert body.rstrip().endswith('`vl fetch d "a.pdf"`.')
    assert drive.cut("short\n", "x") == ("short\n", "full")
    assert drive.cut("  \n\n", "x")[1] == "no text"


# ---------------------------------------------------------------- real runs, with a local folder as the "Drive"

@pytest.fixture
def local_drive(tmp_path, monkeypatch):
    uv_cache = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True).stdout.strip() if shutil.which("uv") else ""
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
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
    cfg = Config()
    cfg.vaults["mixim-drive"] = Vault("mixim-drive", vault)
    settings = {"kind": "drive", "vault": "mixim-drive", "remote": str(src), "max_size": "200K"}
    cfg.plugins["mixim-drive"] = settings
    return cfg, settings, cfg.vaults["mixim-drive"], src


def note(vault, rel):
    return drive.parse_note((vault.path / rel).read_text())


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


@needs_tools
def test_a_run_writes_one_note_per_file(local_drive):
    cfg, settings, vault, _ = local_drive
    assert drive.run(cfg, settings, vault) == "10 new, 0 changed, 0 moved, 0 deleted"
    expected = ["Finance/Runway.xlsx.md", "Legal/Old/scan.pdf.md", "Legal/contract.docx.md", "Team Docs/Plan Q3.md",
                "archive.zip.md", "blank.txt.md", "broken.pdf.md", "deck.pptx.md", "huge.pdf.md", "notes.txt.md", "readme.md"]
    assert sorted(snapshot(vault.path)) == expected

    meta, _, body = note(vault, "Finance/Runway.xlsx.md")
    assert meta["title"] == "Runway" and meta["type"] == "drive-file" and meta["source"] == "mixim-drive"
    assert meta["path"] == "Finance/Runway.xlsx" and meta["text"] == "full" and meta["converter"] == drive.CONVERTER
    assert meta["fetch"] == 'vl fetch mixim-drive "Finance/Runway.xlsx"'
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
        assert body.count("\n") == 1 and "vl fetch mixim-drive" in body
    assert note(vault, "broken.pdf.md")[0]["text"].startswith("failed: ")
    assert (vault.path / "readme.md").read_text() == "# By hand\n"


@needs_tools
def test_a_second_run_changes_nothing_then_follows_drive(local_drive):
    cfg, settings, vault, src = local_drive
    drive.run(cfg, settings, vault)
    before = snapshot(vault.path)
    assert drive.run(cfg, settings, vault) == "0 new, 0 changed, 0 moved, 0 deleted"
    assert snapshot(vault.path) == before

    (src / "notes.txt").write_text("new words\n")
    (src / "Legal" / "Old" / "scan.pdf").unlink()
    assert drive.run(cfg, settings, vault) == "0 new, 1 changed, 0 moved, 1 deleted"
    assert note(vault, "notes.txt.md")[2] == "new words\n"
    assert not (vault.path / "Legal" / "Old").exists()  # empty folders go too
    assert (vault.path / "Legal" / "contract.docx.md").exists()


@needs_tools
def test_rebuild_converts_again_and_keeps_basic_memorys_keys(local_drive):
    cfg, settings, vault, _ = local_drive
    drive.run(cfg, settings, vault)
    path = vault.path / "notes.txt.md"
    path.write_text(path.read_text().replace("---\n\n", "tags:\n- misc\n---\n\n", 1).replace("plain words", "edited"))
    assert drive.run(cfg, settings, vault, rebuild=True) == "0 new, 10 changed, 0 moved, 0 deleted"
    _, extra, body = note(vault, "notes.txt.md")
    assert body == "plain words\n" and extra == ["tags:", "- misc"]


@needs_tools
def test_a_move_keeps_the_body_and_basic_memorys_keys(local_drive, monkeypatch):
    cfg, settings, vault, _ = local_drive
    listing = [F("Finance/Runway.xlsx", "1RUNWAY", mime="text/plain")]
    monkeypatch.setattr(drive, "_list", lambda settings: listing)
    monkeypatch.setattr(drive, "_download", lambda *a: pytest.fail("a move downloads nothing"))
    meta = {"title": "Runway", "type": "drive-file", "source": "mixim-drive", "id": "1RUNWAY",
            "path": "Finance/Runway.xlsx", "modified": listing[0].modified, "md5": "m1", "converter": drive.CONVERTER}
    (vault.path / "Finance").mkdir()
    (vault.path / "Finance" / "Runway.xlsx.md").write_text(drive.render_note(meta, ["tags:", "- cash"], "edited by BM\n"))
    listing[0] = F("Archive/2024 Runway.xlsx", "1RUNWAY", mime="text/plain")
    assert drive.run(cfg, settings, vault) == "0 new, 0 changed, 1 moved, 0 deleted"
    assert not (vault.path / "Finance").exists()
    meta, extra, body = note(vault, "Archive/2024 Runway.xlsx.md")
    assert meta["path"] == "Archive/2024 Runway.xlsx" and meta["title"] == "2024 Runway"
    assert meta["fetch"] == 'vl fetch mixim-drive "Archive/2024 Runway.xlsx"'
    assert meta["url"] == "https://drive.google.com/open?id=1RUNWAY"
    assert extra == ["tags:", "- cash"] and body == "edited by BM\n"


@needs_tools
def test_a_missing_drive_fails_and_touches_nothing(local_drive):
    cfg, settings, vault, src = local_drive
    shutil.rmtree(src)
    with pytest.raises(VlError, match="rclone"):
        drive.run(cfg, settings, vault)
    assert sorted(snapshot(vault.path)) == ["readme.md"]


@needs_tools
def test_two_files_that_swap_names_keep_their_notes(local_drive, monkeypatch):
    cfg, settings, vault, _ = local_drive
    listing = [F("a.pdf", "1AAAAA", mime="text/plain"), F("b.pdf", "2BBBBB", mime="text/plain")]
    monkeypatch.setattr(drive, "_list", lambda settings: listing)
    for f, body in zip(listing, ("about A\n", "about B\n")):
        meta = {"source": "mixim-drive", "id": f.id, "path": f.path, "modified": f.modified, "md5": f.md5,
                "converter": drive.CONVERTER, "text": "full"}
        (vault.path / f"{f.path}.md").write_text(drive.render_note(meta, [], body))
    listing[:] = [F("b.pdf", "1AAAAA", mime="text/plain"), F("a.pdf", "2BBBBB", mime="text/plain")]
    assert drive.run(cfg, settings, vault) == "0 new, 0 changed, 2 moved, 0 deleted"
    assert note(vault, "b.pdf.md")[0]["id"] == "1AAAAA" and note(vault, "b.pdf.md")[2] == "about A\n"
    assert note(vault, "a.pdf.md")[0]["id"] == "2BBBBB" and note(vault, "a.pdf.md")[2] == "about B\n"


def test_making_a_remote_never_shows_its_token(monkeypatch):
    """`rclone config create` prints the new remote, token included, to stdout."""
    import subprocess as sp

    calls = []
    monkeypatch.setattr(drive, "_binary", lambda: "/bin/rc")
    monkeypatch.setattr(drive, "config_file", lambda: "/x/rc.conf")
    monkeypatch.setattr(drive, "_config_dump", lambda: {})
    monkeypatch.setattr(sp, "run", lambda cmd, **kw: calls.append((cmd, kw)) or sp.CompletedProcess(cmd, 0))
    drive.make_remote("vl-x", None, None)
    cmd, kw = calls[0]
    assert cmd[-4:] == ["create", "vl-x", "drive", "scope=drive.readonly"]
    assert kw.get("stdout") == sp.DEVNULL  # the browser link and errors are on stderr, which stays
    assert "stderr" not in kw and "capture_output" not in kw
