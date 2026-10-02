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
def mixim(fake_github, computer):
    gh = fake_github
    gh.org("mixim-ai", ["alice", "bob"])
    gh.vault("mixim-ai/vault-public", ["alice", "bob"],
             'about = "Notes everyone at Mixim can see."\nnotes_from = ["mixim-ai/marketing"]\n')
    gh.vault("mixim-ai/vault-private", ["alice"], 'about = "Founders\' notes."\n')
    gh.repo("mixim-ai/vault-old", ["alice", "bob"], {"README.md": "no vault.toml, so not a vault"})
    gh.repo("mixim-ai/marketing", ["alice", "bob"], {"README.md": "code"})
    return gh


def test_init_makes_your_personal_vault_on_this_computer_only(mixim, computer):
    assert vl("init") == 0
    personal = vaults_dir() / "alice" / "vault-alice-personal"
    assert (personal / ".git").is_dir() and origin(personal) is None
    info, problems = vaults.parse_vault_toml((personal / "vault.toml").read_text())
    assert problems == [] and info.about == "alice's personal notes."
    assert "alice/vault-alice-personal" not in mixim.load()["repos"]  # not on GitHub
    assert config.config_path().exists()
    assert config.load_file().basic_memory is False
    settings = json.loads((computer / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("vl hook")
    data = runtime.load()
    assert data["me"] == "alice" and data["default"] == {"writes": "alice-personal", "reads": []}


def test_init_publish_puts_the_personal_vault_on_github(mixim, computer):
    assert vl("init", "--publish") == 0
    personal = vaults_dir() / "alice" / "vault-alice-personal"
    assert origin(personal) == mixim.url("alice/vault-alice-personal")
    assert mixim.load()["repos"]["alice/vault-alice-personal"] == {"push": ["alice"]}


def test_init_clones_a_personal_vault_published_from_another_computer(mixim, computer):
    mixim.vault("alice/vault-alice-personal", ["alice"], 'about = "From my laptop."\n')
    assert vl("init") == 0
    personal = vaults_dir() / "alice" / "vault-alice-personal"
    assert origin(personal) == mixim.url("alice/vault-alice-personal")
    assert vaults.parse_vault_toml((personal / "vault.toml").read_text())[0].about == "From my laptop."


def test_org_join_clones_the_vaults_you_can_access(mixim, computer):
    assert vl("org", "join", "mixim-ai") == 0  # runs `vl init` first
    found = vaults.on_disk()
    assert sorted(found) == ["alice/vault-alice-personal", "mixim-ai/vault-alice-personal",
                             "mixim-ai/vault-private", "mixim-ai/vault-public"]
    assert found["mixim-ai/vault-public"].remote == mixim.url("mixim-ai/vault-public")
    assert found["mixim-ai/vault-alice-personal"].remote is None  # yours, on this computer only
    owners = runtime.load()["owners"]
    assert owners["mixim-ai"]["personal"] == "mixim-ai-alice-personal"
    assert owners["mixim-ai"]["notes_from"] == {"mixim-ai/marketing": "mixim-ai-public"}


def test_bob_only_gets_what_github_gives_him(mixim, computer):
    mixim.login("bob")
    assert vl("org", "join", "mixim-ai") == 0
    assert sorted(vaults.on_disk()) == ["bob/vault-bob-personal", "mixim-ai/vault-bob-personal",
                                        "mixim-ai/vault-public"]


def test_joining_twice_picks_up_new_vaults(mixim, computer):
    vl("org", "join", "mixim-ai")
    mixim.vault("mixim-ai/vault-design", ["alice"])
    assert vl("org", "join", "mixim-ai") == 0
    assert "mixim-ai/vault-design" in vaults.on_disk()


def test_joining_an_owner_that_doesnt_exist(mixim, computer, capsys):
    vl("init")
    assert vl("org", "join", "no-such-org") == 1
    assert "no-such-org" in capsys.readouterr().err
    assert not (vaults_dir() / "no-such-org").exists()


def test_org_leave_keeps_the_files_unless_asked(mixim, computer, capsys):
    vl("org", "join", "mixim-ai")
    assert vl("org", "leave", "mixim-ai") == 0
    assert not (vaults_dir() / "mixim-ai").exists()
    left = vl_home() / "left" / "mixim-ai"
    assert (left / "vault-public" / "vault.toml").exists()
    assert "never published" in capsys.readouterr().out  # the personal vault's notes live only there
    assert "mixim-ai" not in runtime.load()["owners"]
    vl("org", "join", "mixim-ai")
    assert vl("org", "leave", "mixim-ai", "--delete-files") == 0
    assert not (vaults_dir() / "mixim-ai").exists()


def test_you_cant_leave_your_own_account(mixim, computer, capsys):
    vl("init")
    assert vl("org", "leave", "alice") == 1
    assert "your own account" in capsys.readouterr().err


def test_vault_create_makes_a_vault_on_this_computer(mixim, computer):
    vl("org", "join", "mixim-ai")
    assert vl("vault", "create", "mixim-ai/vault-founders", "--about", "Founders' notes.",
              "--notes_from", "mixim-ai/jorge-ip-theft", "--notes_from", "Mixim-AI/Legal") == 0
    path = vaults_dir() / "mixim-ai" / "vault-founders"
    assert (path / ".git").is_dir() and origin(path) is None
    assert "mixim-ai/vault-founders" not in mixim.load()["repos"]
    info, problems = vaults.parse_vault_toml((path / "vault.toml").read_text())
    assert problems == [] and info.about == "Founders' notes."
    assert info.notes_from == ["mixim-ai/jorge-ip-theft", "mixim-ai/legal"]
    data = runtime.load()
    assert data["vaults"]["mixim-ai-founders"]["audience"]["kind"] == "me"
    assert "mixim-ai-founders" in data["owners"]["mixim-ai"]["vaults"]
    assert data["owners"]["mixim-ai"]["notes_from"]["mixim-ai/jorge-ip-theft"] == "mixim-ai-founders"
    # The daily check doesn't think it was lost: it was never on GitHub.
    state = json.loads(audience.state_path().read_text())
    state["checked_at"] = 0
    audience.state_path().write_text(json.dumps(state))
    assert vl("sync") == 0
    assert "mixim-ai-founders" in runtime.load()["owners"]["mixim-ai"]["vaults"]


def test_vault_create_publish_makes_a_private_repo(mixim, computer):
    vl("org", "join", "mixim-ai")
    assert vl("vault", "create", "mixim-ai/vault-design", "--about", "Design notes.", "--publish") == 0
    path = vaults_dir() / "mixim-ai" / "vault-design"
    assert origin(path) == mixim.url("mixim-ai/vault-design")
    assert vaults.parse_vault_toml((path / "vault.toml").read_text())[0].about == "Design notes."
    assert mixim.load()["repos"]["mixim-ai/vault-design"] == {"push": ["alice"]}
    assert "mixim-ai-design" in runtime.load()["vaults"]


def test_any_local_vault_can_be_published(mixim, computer):
    vl("org", "join", "mixim-ai")
    vl("vault", "create", "mixim-ai/vault-founders")
    assert vl("vault", "publish", "mixim-ai/vault-founders") == 0
    assert origin(vaults_dir() / "mixim-ai" / "vault-founders") == mixim.url("mixim-ai/vault-founders")


def test_there_is_no_local_owner(mixim, computer, capsys):
    vl("init")
    assert vl("vault", "create", "local/recipes") == 1
    assert "start with vault-" in capsys.readouterr().err
    assert vl("vault", "create", "alice/vault-recipes") == 0
    assert "alice-recipes" in runtime.load()["owners"]["alice"]["vaults"]


@pytest.mark.parametrize("name, message", [
    ("mixim-ai/design", "start with vault-"),
    ("other-org/vault-x", "vl org join other-org"),
    ("mixim-ai/vault-public", "already"),
    ("mixim-ai/vault-notes --notes_from nope", "OWNER/REPO"),
    ("nope", "OWNER/vault-NAME"),
])
def test_vault_create_refuses(mixim, computer, capsys, name, message):
    vl("org", "join", "mixim-ai")
    assert vl("vault", "create", *name.split()) != 0
    assert message in capsys.readouterr().err


def test_vault_publish(mixim, computer, capsys):
    vl("org", "join", "mixim-ai")
    assert vl("vault", "publish", "mixim-ai/vault-alice-personal") == 0
    path = vaults_dir() / "mixim-ai" / "vault-alice-personal"
    assert origin(path) == mixim.url("mixim-ai/vault-alice-personal")
    assert mixim.load()["repos"]["mixim-ai/vault-alice-personal"] == {"push": ["alice"]}
    assert vl("vault", "publish", "mixim-ai/vault-alice-personal") == 1
    assert "already on GitHub" in capsys.readouterr().err
    assert vl("vault", "publish", "mixim-ai/vault-public") == 1
    assert "already on GitHub" in capsys.readouterr().err


def test_a_published_personal_vault_is_found_by_whoever_can_access_it(mixim, computer):
    vl("org", "join", "mixim-ai")
    vl("vault", "publish", "mixim-ai/vault-alice-personal")
    data = mixim.load()
    data["repos"]["mixim-ai/vault-alice-personal"]["read"] = ["bob"]  # alice shared it
    mixim.save(data)
    assert "mixim-ai/vault-alice-personal" in vaults_github_list("mixim-ai", "bob", mixim)


def vaults_github_list(owner, who, gh):
    from vaultlines import github

    gh.login(who)
    try:
        return github.vault_repos(owner)
    finally:
        gh.login("alice")


def test_sync_finds_new_vaults_and_stops_syncing_lost_ones(mixim, computer, capsys):
    vl("org", "join", "mixim-ai")
    mixim.vault("mixim-ai/vault-new", ["alice"])
    data = mixim.load()
    data["repos"]["mixim-ai/vault-private"] = {"push": ["bob"]}  # alice lost access
    mixim.save(data)
    state = json.loads(audience.state_path().read_text())
    state["checked_at"] = 0  # a day passes
    audience.state_path().write_text(json.dumps(state))
    capsys.readouterr()
    assert vl("sync") == 0
    out = capsys.readouterr().out
    assert (vaults_dir() / "mixim-ai" / "vault-new" / ".git").is_dir()
    assert "mixim-ai/vault-private: no access on GitHub any more" in out
    assert (vaults_dir() / "mixim-ai" / "vault-private" / "vault.toml").exists()  # its files stay
    assert vl("status") == 0
    assert "no access on GitHub any more" in capsys.readouterr().out
    assert "mixim-ai-private" not in runtime.load()["owners"]["mixim-ai"]["vaults"]


def test_sync_follows_vault_toml_changes(mixim, computer):
    vl("org", "join", "mixim-ai")
    work = mixim.root / ".work" / "mixim-ai" / "vault-public"
    commit_files(work, {"vault.toml": 'about = "Everyone."\nnotes_from = ["mixim-ai/studio"]\n'}, "studio")
    assert vl("sync") == 0
    assert runtime.load()["owners"]["mixim-ai"]["notes_from"] == {"mixim-ai/studio": "mixim-ai-public"}


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


def test_apply_points_basic_memory_at_each_repos_vault(mixim, computer, fake_basic_memory, tmp_path):
    vl("org", "join", "mixim-ai")
    assert sorted(fake_basic_memory) == ["alice-personal", "mixim-ai-alice-personal", "mixim-ai-private",
                                         "mixim-ai-public"]
    user = json.loads((computer / ".claude" / "settings.json").read_text())
    assert user["basicMemory"]["primaryProject"] == "alice-personal"
    # Claude ran in two clones (the hook records them).
    marketing = tmp_path / "code" / "marketing"
    studio = tmp_path / "code" / "studio"
    for repo, url in ((marketing, mixim.url("mixim-ai/marketing")), (studio, mixim.url("mixim-ai/studio"))):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
    from vaultlines.util import clones_path

    clones_path().write_text(json.dumps({str(marketing): "mixim-ai/marketing", str(studio): "mixim-ai/studio"}))
    assert vl("apply") == 0
    block = json.loads((marketing / ".claude" / "settings.local.json").read_text())["basicMemory"]
    assert block["primaryProject"] == "mixim-ai-public"
    assert json.loads((studio / ".claude" / "settings.local.json").read_text())["basicMemory"]["primaryProject"] \
        == "mixim-ai-alice-personal"
    # vault-public now takes studio's notes too: apply follows.
    work = mixim.root / ".work" / "mixim-ai" / "vault-public"
    commit_files(work, {"vault.toml": 'notes_from = ["mixim-ai/marketing", "mixim-ai/studio"]\n'}, "studio")
    assert vl("sync") == 0
    assert json.loads((studio / ".claude" / "settings.local.json").read_text())["basicMemory"]["primaryProject"] \
        == "mixim-ai-public"


def test_leaving_an_owner_removes_its_basic_memory_projects(mixim, computer, fake_basic_memory):
    vl("org", "join", "mixim-ai")
    vl("org", "leave", "mixim-ai")
    assert sorted(fake_basic_memory) == ["alice-personal"]


def test_turning_basic_memory_off_removes_vls_blocks(mixim, computer, fake_basic_memory, tmp_path):
    from vaultlines.util import clones_path

    vl("org", "join", "mixim-ai")
    repo = tmp_path / "marketing"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", mixim.url("mixim-ai/marketing")], check=True)
    clones_path().write_text(json.dumps({str(repo): "mixim-ai/marketing"}))
    vl("apply")
    assert (repo / ".claude" / "settings.local.json").exists()
    config.set_basic_memory(False)
    vl("apply")
    assert "basicMemory" not in json.loads((repo / ".claude" / "settings.local.json").read_text())
    assert "basicMemory" not in json.loads((computer / ".claude" / "settings.json").read_text())


def test_session_checkpoints_never_leave_this_computer(mixim, computer):
    """vault-public was made by hand on GitHub: it has no .gitignore from vl."""
    vl("org", "join", "mixim-ai")
    path = vaults_dir() / "mixim-ai" / "vault-public"
    (path / "sessions").mkdir()
    (path / "sessions" / "checkpoint.md").write_text("private\n")
    (path / "note.md").write_text("shared\n")
    assert vl("sync") == 0
    shown = subprocess.run(["git", "--git-dir", str(mixim.root / "mixim-ai" / "vault-public.git"), "ls-tree", "-r",
                            "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    assert "note.md" in shown and "sessions/checkpoint.md" not in shown


def test_two_edits_to_one_note_keep_both_in_a_vault_made_by_hand(mixim, computer, tmp_path):
    vl("org", "join", "mixim-ai")
    path = vaults_dir() / "mixim-ai" / "vault-public"
    (path / "n.md").write_text("start\n")
    vl("sync")
    work = mixim.root / ".work" / "mixim-ai" / "vault-public"
    subprocess.run(["git", "-C", str(work), "pull", "-q", "origin", "main"], check=True, capture_output=True)
    (work / "n.md").write_text("start\ntheirs\n")
    commit_files(work, {}, "theirs")
    (path / "n.md").write_text("start\nmine\n")
    assert vl("sync") == 0
    text = (path / "n.md").read_text()
    assert "theirs" in text and "mine" in text
