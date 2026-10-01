"""`vl fetch`: one original from Drive, on demand, into the fetch folder."""

from __future__ import annotations

import os
import shutil
import time

import pytest

from vaultlines import cli
from vaultlines.config import Config, Vault
from vaultlines.plugins import drive
from vaultlines.util import VlError, fetch_dir

needs_rclone = pytest.mark.skipif(not shutil.which("rclone"), reason="rclone isn't installed")


@pytest.mark.parametrize("path", ["/etc/passwd", "../x.pdf", "a/../../x.pdf", "a/..", "", "  ", "-x", "a\nb"])
def test_bad_paths_are_refused(path):
    with pytest.raises(VlError):
        drive.check_fetch_path(path)


@pytest.mark.parametrize("path", ["Runway.xlsx", "Finance/2024/Runway.xlsx", "Team Docs/Plan Q3.md", "a..b.pdf"])
def test_good_paths(path):
    drive.check_fetch_path(path)


def test_source_path_starts_with_the_remote():
    assert drive.source_path("mixim:", "a/b.pdf") == "mixim:a/b.pdf"
    assert drive.source_path("mixim,team_drive=0AB:Legal", "a.pdf") == "mixim,team_drive=0AB:Legal/a.pdf"
    assert drive.source_path("mixim:Legal/", "a.pdf") == "mixim:Legal/a.pdf"


def test_fetch_dir_follows_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert fetch_dir("mixim-drive") == tmp_path / "vaultlines" / "fetch" / "mixim-drive"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("VAULTLINES_CONFIG", str(tmp_path / "config.toml"))
    src = tmp_path / "drive"
    (src / "Finance").mkdir(parents=True)
    (src / "Finance" / "Runway.xlsx").write_bytes(b"PK\x03\x04 original bytes")
    os.utime(src / "Finance" / "Runway.xlsx", (0, 0))  # Drive's modification time is long ago
    cfg = Config()
    cfg.vaults["mixim-drive"] = Vault("mixim-drive", tmp_path / "vault")
    cfg.vaults["personal"] = Vault("personal", tmp_path / "personal")
    cfg.plugins["mixim-drive"] = {"kind": "drive", "vault": "mixim-drive", "remote": str(src)}
    return cfg, src


@needs_rclone
def test_fetch_copies_one_file(setup, capsys):
    cfg, _ = setup
    settings = cfg.plugins["mixim-drive"]
    out = drive.fetch(cfg, settings, cfg.vaults["mixim-drive"], "Finance/Runway.xlsx", fetch_dir("mixim-drive"))
    assert out == fetch_dir("mixim-drive") / "Finance" / "Runway.xlsx"
    assert out.read_bytes() == b"PK\x03\x04 original bytes"
    assert time.time() - out.stat().st_mtime < 60  # so `vl sync` keeps it for a day
    assert not os.access(out, os.W_OK)  # a read-only copy
    again = drive.fetch(cfg, settings, cfg.vaults["mixim-drive"], "Finance/Runway.xlsx", fetch_dir("mixim-drive"))
    assert again == out


@needs_rclone
def test_fetch_refuses_folders_and_missing_files(setup):
    cfg, _ = setup
    settings, vault = cfg.plugins["mixim-drive"], cfg.vaults["mixim-drive"]
    with pytest.raises(VlError, match="folder"):
        drive.fetch(cfg, settings, vault, "Finance", fetch_dir("mixim-drive"))
    with pytest.raises(VlError, match="not found|No file"):
        drive.fetch(cfg, settings, vault, "Finance/nope.pdf", fetch_dir("mixim-drive"))


def test_fetch_needs_a_source_on_this_computer(setup, monkeypatch):
    cfg, _ = setup
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    with pytest.raises(VlError, match="No source fills 'personal' on this computer"):
        cli.cmd_fetch(cli.build_parser().parse_args(["fetch", "personal", "x.pdf"]))


def test_old_fetched_files_are_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    old, new = fetch_dir("d") / "a" / "old.pdf", fetch_dir("d") / "new.pdf"
    for p in (old, new):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        p.chmod(0o444)
    os.utime(old, (time.time() - 2 * 86400,) * 2)
    assert cli.clean_fetched() == 1
    assert not old.exists() and not old.parent.exists() and new.exists()
