"""Vaults: vault.toml, and what's on disk."""

import subprocess

import pytest

from vaultlines import vaults
from vaultlines.vaults import parse_vault_toml


@pytest.mark.parametrize("vault_id, ok", [
    ("acme/vault-public", True),
    ("acme/recipes", True),
    ("ACME/vault-public", False),  # vl writes IDs in lowercase
    ("acme", False),
    ("acme/vault-public/x", False),
    ("../vault-x", False),
    ("acme/..", False),
])
def test_valid_id(vault_id, ok):
    assert vaults.valid_id(vault_id) is ok


def test_parse_a_full_vault_toml():
    info, problems = parse_vault_toml('''
about      = "Notes everyone at Acme can see."
notes_from = ["acme/acme", "ACME/Marketing"]

[source]
kind         = "gdrive"
shared_drive = "0AHF"
''')
    assert problems == []
    assert info.about == "Notes everyone at Acme can see."
    assert info.notes_from == ["acme/acme", "acme/marketing"]  # GitHub names ignore case
    assert info.source == {"kind": "gdrive", "shared_drive": "0AHF"}


def test_an_empty_vault_toml_is_fine():
    info, problems = parse_vault_toml("")
    assert (info.about, info.notes_from, info.source, problems) == ("", [], None, [])


@pytest.mark.parametrize("text, problem", [
    ("about = ", "vault.toml:"),
    ("about = 5", "about: should be text"),
    ('notes_from = "acme/x"', "notes_from: should be a list"),
    ('notes_from = ["not a repo"]', "notes_from: 'not a repo' isn't OWNER/REPO"),
    ('colour = "red"', "colour: unknown key"),
    ('source = "gdrive"', "source: should be a table"),
    ('[source]\nshared_drive = "x"', "source.kind: missing"),
])
def test_problems_are_reported_not_raised(text, problem):
    _, problems = parse_vault_toml(text)
    assert any(problem in p for p in problems), problems


def test_a_source_table_without_a_kind_still_marks_the_vault_as_filled():
    info, _ = parse_vault_toml('[source]\nshared_drive = "x"')
    assert info.source == {"shared_drive": "x"}


def test_render_round_trips():
    text = vaults.render_vault_toml('Kabir\'s "personal" notes.', ["kabir/blog"])
    info, problems = parse_vault_toml(text)
    assert problems == []
    assert (info.about, info.notes_from) == ('Kabir\'s "personal" notes.', ["kabir/blog"])


def _repo(path, origin=None):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)


def test_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("VL_HOME", str(tmp_path))
    root = tmp_path / "vaults"
    _repo(root / "acme" / "vault-public", "https://github.com/acme/vault-public.git")
    (root / "acme" / "vault-public" / "vault.toml").write_text('about = "Everyone."\nnotes_from = ["acme/x"]\n')
    _repo(root / "acme" / "vault-kabir-personal")
    _repo(root / "kabir" / "vault-recipes")
    (root / "acme" / "not-a-repo").mkdir()
    (root / "empty-owner").mkdir()
    (root / ".DS_Store").write_text("")
    found = vaults.on_disk()
    assert sorted(found) == ["acme/vault-kabir-personal", "acme/vault-public", "kabir/vault-recipes"]
    public = found["acme/vault-public"]
    assert (public.owner, public.repo, public.about, public.notes_from) == ("acme", "vault-public", "Everyone.", ["acme/x"])
    assert public.remote == "https://github.com/acme/vault-public.git"
    assert found["acme/vault-kabir-personal"].remote is None
    assert vaults.joined() == ["acme", "empty-owner", "kabir"]


def test_personal_id():
    assert vaults.personal_id("acme", "kabir") == "acme/vault-kabir-personal"
    assert vaults.personal_id("kabir", "kabir") == "kabir/vault-kabir-personal"


@pytest.mark.parametrize("url, found", [
    ("https://github.com/acme/vault-public.git", "acme/vault-public"),
    ("git@github.com:ACME/vault-public", "acme/vault-public"),
    ("ssh://git@github.com/acme/vault-public/", "acme/vault-public"),
    ("https://gitlab.com/acme/vault-public.git", None),
    ("file:///tmp/remotes/acme/vault-public.git", None),
])
def test_remote_id(url, found):
    assert vaults.remote_id(url) == found


def test_file_remotes_count_in_tests(monkeypatch):
    monkeypatch.setenv("VL_TEST_REMOTES", "1")
    assert vaults.remote_id("file:///tmp/remotes/acme/vault-public.git") == "acme/vault-public"


def test_a_vault_is_named_only_by_its_id(tmp_path, monkeypatch):
    from vaultlines.config import Config
    from vaultlines.util import VlError

    v = vaults.Vault("acme/vault-public", tmp_path)
    cfg = Config(vaults={"acme/vault-public": v, "kabir/vault-recipes": vaults.Vault("kabir/vault-recipes", tmp_path)})
    assert cfg.vault("ACME/vault-public ") is v
    with pytest.raises(VlError) as e:
        cfg.vault("acme-public")
    assert str(e.value) == ("No vault 'acme-public' on this computer. A vault is OWNER/REPO, like one of these: "
                            "acme/vault-public, kabir/vault-recipes")
