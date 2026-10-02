"""Vaults: vault.toml, short names, and what's on disk."""

import subprocess

import pytest

from vaultlines import vaults
from vaultlines.vaults import parse_vault_toml, short_name, short_names


@pytest.mark.parametrize("vault_id, short", [
    ("mixim-ai/vault-public", "mixim-ai-public"),
    ("kabir/vault-kabir-personal", "kabir-personal"),
    ("mixim-ai/vault-kabir-personal", "mixim-ai-kabir-personal"),
    ("mixim-ai/vault-mixim-ai-hq", "mixim-ai-hq"),
    ("acme/vault-acme", "acme"),
    ("acme/recipes", "acme-recipes"),
    ("acme/vault-acmecorp", "acme-acmecorp"),  # only a whole word counts as the owner
])
def test_short_name(vault_id, short):
    assert short_name(vault_id) == short


def test_short_names_never_collide():
    found = short_names(["a-b/vault-c", "a/vault-b-c", "x/vault-y"])
    assert found["a-b/vault-c"] == "a-b-c"
    assert found["a/vault-b-c"] == "a-vault-b-c"
    assert found["x/vault-y"] == "x-y"
    assert len(set(found.values())) == 3


@pytest.mark.parametrize("vault_id, ok", [
    ("mixim-ai/vault-public", True),
    ("acme/recipes", True),
    ("Mixim-AI/vault-public", False),  # vl writes IDs in lowercase
    ("mixim-ai", False),
    ("mixim-ai/vault-public/x", False),
    ("../vault-x", False),
    ("mixim-ai/..", False),
])
def test_valid_id(vault_id, ok):
    assert vaults.valid_id(vault_id) is ok


def test_parse_a_full_vault_toml():
    info, problems = parse_vault_toml('''
about      = "Notes everyone at Mixim can see."
notes_from = ["mixim-ai/mixim", "Mixim-AI/Marketing"]

[source]
kind         = "gdrive"
shared_drive = "0AHF"
''')
    assert problems == []
    assert info.about == "Notes everyone at Mixim can see."
    assert info.notes_from == ["mixim-ai/mixim", "mixim-ai/marketing"]  # GitHub names ignore case
    assert info.source == {"kind": "gdrive", "shared_drive": "0AHF"}


def test_an_empty_vault_toml_is_fine():
    info, problems = parse_vault_toml("")
    assert (info.about, info.notes_from, info.source, problems) == ("", [], None, [])


@pytest.mark.parametrize("text, problem", [
    ("about = ", "vault.toml:"),
    ("about = 5", "about: should be text"),
    ('notes_from = "mixim-ai/x"', "notes_from: should be a list"),
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
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path))
    root = tmp_path / "vaults"
    _repo(root / "mixim-ai" / "vault-public", "https://github.com/mixim-ai/vault-public.git")
    (root / "mixim-ai" / "vault-public" / "vault.toml").write_text('about = "Everyone."\nnotes_from = ["mixim-ai/x"]\n')
    _repo(root / "mixim-ai" / "vault-kabir-personal")
    _repo(root / "kabir" / "vault-recipes")
    (root / "mixim-ai" / "not-a-repo").mkdir()
    (root / "empty-owner").mkdir()
    (root / ".DS_Store").write_text("")
    found = vaults.on_disk()
    assert sorted(found) == ["kabir/vault-recipes", "mixim-ai/vault-kabir-personal", "mixim-ai/vault-public"]
    public = found["mixim-ai/vault-public"]
    assert (public.owner, public.repo, public.about, public.notes_from) == ("mixim-ai", "vault-public", "Everyone.", ["mixim-ai/x"])
    assert public.remote == "https://github.com/mixim-ai/vault-public.git"
    assert found["mixim-ai/vault-kabir-personal"].remote is None
    assert vaults.joined() == ["empty-owner", "kabir", "mixim-ai"]


def test_personal_id():
    assert vaults.personal_id("mixim-ai", "kabir") == "mixim-ai/vault-kabir-personal"
    assert vaults.personal_id("kabir", "kabir") == "kabir/vault-kabir-personal"


@pytest.mark.parametrize("url, found", [
    ("https://github.com/mixim-ai/vault-public.git", "mixim-ai/vault-public"),
    ("git@github.com:Mixim-AI/vault-public", "mixim-ai/vault-public"),
    ("ssh://git@github.com/mixim-ai/vault-public/", "mixim-ai/vault-public"),
    ("https://gitlab.com/mixim-ai/vault-public.git", None),
    ("file:///tmp/remotes/mixim-ai/vault-public.git", None),
])
def test_remote_id(url, found):
    assert vaults.remote_id(url) == found


def test_file_remotes_count_in_tests(monkeypatch):
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    assert vaults.remote_id("file:///tmp/remotes/mixim-ai/vault-public.git") == "mixim-ai/vault-public"
