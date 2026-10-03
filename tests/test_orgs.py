"""`vl init`, `vl org join/leave`, `vl vault create/publish`: GitHub decides who gets which vault."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import commit_files, vl

from vaultlines import audience, config, runtime, vaults
from vaultlines.util import vaults_dir, vl_home


def origin(path) -> str | None:
    out = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True)
    return out.stdout.strip() or None


@pytest.fixture
def acme(fake_github, computer):
    gh = fake_github
    gh.org("acme", ["alice", "bob"])
    gh.vault("acme/vault-public", ["alice", "bob"],
             'about = "Notes everyone at Acme can see."\nnotes_from = ["acme/marketing"]\n')
    gh.vault("acme/vault-private", ["alice"], 'about = "Founders\' notes."\n')
    gh.repo("acme/vault-old", ["alice", "bob"], {"README.md": "no vault.toml, so not a vault"})
    gh.repo("acme/marketing", ["alice", "bob"], {"README.md": "code"})
    return gh


def test_init_makes_your_personal_vault_on_this_computer_only(acme, computer):
    assert vl("init") == 0
    personal = vaults_dir() / "alice" / "vault-alice-personal"
    assert (personal / ".git").is_dir() and origin(personal) is None
    info, problems = vaults.parse_vault_toml((personal / "vault.toml").read_text())
    assert problems == [] and info.about == "alice's personal notes."
    assert "alice/vault-alice-personal" not in acme.load()["repos"]  # not on GitHub
    assert config.config_path().exists()
    assert config.load_file().basic_memory is False
    settings = json.loads((computer / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("vl hook")
    data = runtime.load()
    assert data["me"] == "alice" and data["default"] == {"writes": "alice-personal", "reads": []}


def test_init_publish_puts_the_personal_vault_on_github(acme, computer):
    assert vl("init", "--publish") == 0
    personal = vaults_dir() / "alice" / "vault-alice-personal"
    assert origin(personal) == acme.url("alice/vault-alice-personal")
    assert acme.load()["repos"]["alice/vault-alice-personal"] == {"push": ["alice"]}


def test_init_clones_a_personal_vault_published_from_another_computer(acme, computer):
    acme.vault("alice/vault-alice-personal", ["alice"], 'about = "From my laptop."\n')
    assert vl("init") == 0
    personal = vaults_dir() / "alice" / "vault-alice-personal"
    assert origin(personal) == acme.url("alice/vault-alice-personal")
    assert vaults.parse_vault_toml((personal / "vault.toml").read_text())[0].about == "From my laptop."


def test_org_join_clones_the_vaults_you_can_access(acme, computer):
    assert vl("org", "join", "acme") == 0  # runs `vl init` first
    found = vaults.on_disk()
    assert sorted(found) == ["acme/vault-alice-personal", "acme/vault-private", "acme/vault-public",
                             "alice/vault-alice-personal"]
    assert found["acme/vault-public"].remote == acme.url("acme/vault-public")
    assert found["acme/vault-alice-personal"].remote is None  # yours, on this computer only
    owners = runtime.load()["owners"]
    assert owners["acme"]["personal"] == "acme-alice-personal"
    assert owners["acme"]["notes_from"] == {"acme/marketing": "acme-public"}


def test_bob_only_gets_what_github_gives_him(acme, computer):
    acme.login("bob")
    assert vl("org", "join", "acme") == 0
    assert sorted(vaults.on_disk()) == ["acme/vault-bob-personal", "acme/vault-public", "bob/vault-bob-personal"]


def test_joining_twice_picks_up_new_vaults(acme, computer):
    vl("org", "join", "acme")
    acme.vault("acme/vault-design", ["alice"])
    assert vl("org", "join", "acme") == 0
    assert "acme/vault-design" in vaults.on_disk()


def test_joining_an_owner_that_doesnt_exist(acme, computer, capsys):
    vl("init")
    assert vl("org", "join", "no-such-org") == 1
    assert "no-such-org" in capsys.readouterr().err
    assert not (vaults_dir() / "no-such-org").exists()


def test_org_leave_keeps_the_files_unless_asked(acme, computer, capsys):
    vl("org", "join", "acme")
    assert vl("org", "leave", "acme") == 0
    assert not (vaults_dir() / "acme").exists()
    left = vl_home() / "left" / "acme"
    assert (left / "vault-public" / "vault.toml").exists()
    assert "never published" in capsys.readouterr().out  # the personal vault's notes live only there
    assert "acme" not in runtime.load()["owners"]
    vl("org", "join", "acme")
    assert vl("org", "leave", "acme", "--delete-files") == 0
    assert not (vaults_dir() / "acme").exists()


def test_you_cant_leave_your_own_account(acme, computer, capsys):
    vl("init")
    assert vl("org", "leave", "alice") == 1
    assert "your own account" in capsys.readouterr().err


def test_vault_create_makes_a_vault_on_this_computer(acme, computer):
    vl("org", "join", "acme")
    assert vl("vault", "create", "acme/vault-founders", "--about", "Founders' notes.",
              "--notes_from", "acme/legal-case", "--notes_from", "ACME/Legal") == 0
    path = vaults_dir() / "acme" / "vault-founders"
    assert (path / ".git").is_dir() and origin(path) is None
    assert "acme/vault-founders" not in acme.load()["repos"]
    info, problems = vaults.parse_vault_toml((path / "vault.toml").read_text())
    assert problems == [] and info.about == "Founders' notes."
    assert info.notes_from == ["acme/legal-case", "acme/legal"]
    data = runtime.load()
    assert data["vaults"]["acme-founders"]["audience"]["kind"] == "me"
    assert "acme-founders" in data["owners"]["acme"]["vaults"]
    assert data["owners"]["acme"]["notes_from"]["acme/legal-case"] == "acme-founders"
    # The daily check doesn't think it was lost: it was never on GitHub.
    state = json.loads(audience.state_path().read_text())
    state["checked_at"] = 0
    audience.state_path().write_text(json.dumps(state))
    assert vl("sync") == 0
    assert "acme-founders" in runtime.load()["owners"]["acme"]["vaults"]


def test_vault_create_publish_makes_a_private_repo(acme, computer):
    vl("org", "join", "acme")
    assert vl("vault", "create", "acme/vault-design", "--about", "Design notes.", "--publish") == 0
    path = vaults_dir() / "acme" / "vault-design"
    assert origin(path) == acme.url("acme/vault-design")
    assert vaults.parse_vault_toml((path / "vault.toml").read_text())[0].about == "Design notes."
    assert acme.load()["repos"]["acme/vault-design"] == {"push": ["alice"]}
    assert "acme-design" in runtime.load()["vaults"]


def test_any_local_vault_can_be_published(acme, computer):
    vl("org", "join", "acme")
    vl("vault", "create", "acme/vault-founders")
    assert vl("vault", "publish", "acme/vault-founders") == 0
    assert origin(vaults_dir() / "acme" / "vault-founders") == acme.url("acme/vault-founders")


def test_there_is_no_local_owner(acme, computer, capsys):
    vl("init")
    assert vl("vault", "create", "local/recipes") == 1
    assert "start with vault-" in capsys.readouterr().err
    assert vl("vault", "create", "alice/vault-recipes") == 0
    assert "alice-recipes" in runtime.load()["owners"]["alice"]["vaults"]


@pytest.mark.parametrize("name, message", [
    ("acme/design", "start with vault-"),
    ("other-org/vault-x", "vl org join other-org"),
    ("acme/vault-public", "already"),
    ("acme/vault-notes --notes_from nope", "OWNER/REPO"),
    ("nope", "OWNER/vault-NAME"),
])
def test_vault_create_refuses(acme, computer, capsys, name, message):
    vl("org", "join", "acme")
    assert vl("vault", "create", *name.split()) != 0
    assert message in capsys.readouterr().err


def test_vault_publish(acme, computer, capsys):
    vl("org", "join", "acme")
    assert vl("vault", "publish", "acme/vault-alice-personal") == 0
    path = vaults_dir() / "acme" / "vault-alice-personal"
    assert origin(path) == acme.url("acme/vault-alice-personal")
    assert acme.load()["repos"]["acme/vault-alice-personal"] == {"push": ["alice"]}
    assert vl("vault", "publish", "acme/vault-alice-personal") == 1
    assert "already on GitHub" in capsys.readouterr().err
    assert vl("vault", "publish", "acme/vault-public") == 1
    assert "already on GitHub" in capsys.readouterr().err


def test_a_published_personal_vault_is_found_by_whoever_can_access_it(acme, computer):
    vl("org", "join", "acme")
    vl("vault", "publish", "acme/vault-alice-personal")
    data = acme.load()
    data["repos"]["acme/vault-alice-personal"]["read"] = ["bob"]  # alice shared it
    acme.save(data)
    assert "acme/vault-alice-personal" in vaults_github_list("acme", "bob", acme)


def vaults_github_list(owner, who, gh):
    from vaultlines import github

    gh.login(who)
    try:
        return github.vault_repos(owner)
    finally:
        gh.login("alice")


def test_sync_finds_new_vaults_and_stops_syncing_lost_ones(acme, computer, capsys):
    vl("org", "join", "acme")
    acme.vault("acme/vault-new", ["alice"])
    data = acme.load()
    data["repos"]["acme/vault-private"] = {"push": ["bob"]}  # alice lost access
    acme.save(data)
    state = json.loads(audience.state_path().read_text())
    state["checked_at"] = 0  # a day passes
    audience.state_path().write_text(json.dumps(state))
    capsys.readouterr()
    assert vl("sync") == 0
    out = capsys.readouterr().out
    assert (vaults_dir() / "acme" / "vault-new" / ".git").is_dir()
    assert "acme/vault-private: no access on GitHub any more" in out
    assert (vaults_dir() / "acme" / "vault-private" / "vault.toml").exists()  # its files stay
    assert vl("status") == 0
    assert "no access on GitHub any more" in capsys.readouterr().out
    assert "acme-private" not in runtime.load()["owners"]["acme"]["vaults"]


def test_sync_follows_vault_toml_changes(acme, computer):
    vl("org", "join", "acme")
    work = acme.root / ".work" / "acme" / "vault-public"
    commit_files(work, {"vault.toml": 'about = "Everyone."\nnotes_from = ["acme/studio"]\n'}, "studio")
    assert vl("sync") == 0
    assert runtime.load()["owners"]["acme"]["notes_from"] == {"acme/studio": "acme-public"}


@pytest.fixture
def fake_basic_memory(monkeypatch):
    """Basic Memory on, without running it: projects are whatever vl registered."""
    from vaultlines import claude
    from vaultlines.plugins import basic_memory as bm

    registered = {}
    monkeypatch.setattr(bm, "projects", lambda settings: dict(registered))
    monkeypatch.setattr(bm, "ensure_project", lambda settings, name, path, current=None: registered.update({name: path.resolve()}))
    monkeypatch.setattr(bm, "remove_project", lambda settings, name: registered.pop(name, None))
    monkeypatch.setattr(bm, "plugin_installed", lambda: True)
    monkeypatch.setattr(bm, "set_default", lambda settings, name: None)
    monkeypatch.setattr(bm, "init", lambda cfg, settings: None)
    monkeypatch.setattr(claude, "add_server", lambda *a, **k: None)
    config.set_basic_memory(True)
    return registered


def test_apply_points_basic_memory_at_each_repos_vault(acme, computer, fake_basic_memory, tmp_path):
    vl("org", "join", "acme")
    assert sorted(fake_basic_memory) == ["acme-alice-personal", "acme-private", "acme-public", "alice-personal"]
    user = json.loads((computer / ".claude" / "settings.json").read_text())
    assert user["basicMemory"]["primaryProject"] == "alice-personal"
    # Claude ran in two clones (the hook records them).
    marketing = tmp_path / "code" / "marketing"
    studio = tmp_path / "code" / "studio"
    for repo, url in ((marketing, acme.url("acme/marketing")), (studio, acme.url("acme/studio"))):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
    from vaultlines.util import clones_path

    clones_path().write_text(json.dumps({str(marketing): "acme/marketing", str(studio): "acme/studio"}))
    assert vl("apply") == 0
    block = json.loads((marketing / ".claude" / "settings.local.json").read_text())["basicMemory"]
    assert block["primaryProject"] == "acme-public"
    assert json.loads((studio / ".claude" / "settings.local.json").read_text())["basicMemory"]["primaryProject"] \
        == "acme-alice-personal"
    # vault-public now takes studio's notes too: apply follows.
    work = acme.root / ".work" / "acme" / "vault-public"
    commit_files(work, {"vault.toml": 'notes_from = ["acme/marketing", "acme/studio"]\n'}, "studio")
    assert vl("sync") == 0
    assert json.loads((studio / ".claude" / "settings.local.json").read_text())["basicMemory"]["primaryProject"] \
        == "acme-public"


def test_leaving_an_owner_removes_its_basic_memory_projects(acme, computer, fake_basic_memory):
    vl("org", "join", "acme")
    vl("org", "leave", "acme")
    assert sorted(fake_basic_memory) == ["alice-personal"]


def test_turning_basic_memory_off_removes_vls_blocks(acme, computer, fake_basic_memory, tmp_path):
    from vaultlines.util import clones_path

    vl("org", "join", "acme")
    repo = tmp_path / "marketing"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", acme.url("acme/marketing")], check=True)
    clones_path().write_text(json.dumps({str(repo): "acme/marketing"}))
    vl("apply")
    assert (repo / ".claude" / "settings.local.json").exists()
    config.set_basic_memory(False)
    vl("apply")
    assert "basicMemory" not in json.loads((repo / ".claude" / "settings.local.json").read_text())
    assert "basicMemory" not in json.loads((computer / ".claude" / "settings.json").read_text())


def test_session_checkpoints_never_leave_this_computer(acme, computer):
    """vault-public was made by hand on GitHub: it has no .gitignore from vl."""
    vl("org", "join", "acme")
    path = vaults_dir() / "acme" / "vault-public"
    (path / "sessions").mkdir()
    (path / "sessions" / "checkpoint.md").write_text("private\n")
    (path / "note.md").write_text("shared\n")
    assert vl("sync") == 0
    shown = subprocess.run(["git", "--git-dir", str(acme.root / "acme" / "vault-public.git"), "ls-tree", "-r",
                            "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    assert "note.md" in shown and "sessions/checkpoint.md" not in shown


def test_two_edits_to_one_note_keep_both_in_a_vault_made_by_hand(acme, computer, tmp_path):
    vl("org", "join", "acme")
    path = vaults_dir() / "acme" / "vault-public"
    (path / "n.md").write_text("start\n")
    vl("sync")
    work = acme.root / ".work" / "acme" / "vault-public"
    subprocess.run(["git", "-C", str(work), "pull", "-q", "origin", "main"], check=True, capture_output=True)
    (work / "n.md").write_text("start\ntheirs\n")
    commit_files(work, {}, "theirs")
    (path / "n.md").write_text("start\nmine\n")
    assert vl("sync") == 0
    text = (path / "n.md").read_text()
    assert "theirs" in text and "mine" in text
