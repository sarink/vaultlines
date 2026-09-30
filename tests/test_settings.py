import json

from vaultlines import claude
from vaultlines.claude import install_hooks, is_our_hook, remove_hooks, update_settings
from vaultlines.hook import MATCHER


def test_update_settings_sets_the_block_and_removes_v2_rules(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text(json.dumps({
        "outputStyle": "Concise",
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["mcp__vl-old", "Bash(rm:*)"],
                        "ask": ["mcp__vl-x__write_note"]},
        "basicMemory": {"primaryProject": "old", "secondaryProjects": ["y"]},
    }))
    update_settings(path, {"primaryProject": "acme", "captureFolder": "sessions", "captureEvents": False})
    data = json.loads(path.read_text())
    assert data["outputStyle"] == "Concise"
    assert data["permissions"] == {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]}
    assert data["basicMemory"] == {"primaryProject": "acme", "captureFolder": "sessions", "captureEvents": False}


def test_update_settings_unset_removes_only_ours(tmp_path):
    path = tmp_path / "settings.local.json"
    update_settings(path, {"primaryProject": "x"})
    update_settings(path, None)
    assert json.loads(path.read_text()) == {}
    other = tmp_path / "missing.json"
    assert update_settings(other, None) is False
    assert not other.exists()


def test_update_settings_is_stable(tmp_path):
    path = tmp_path / "s.json"
    assert update_settings(path, {"primaryProject": "x"}) is True
    assert update_settings(path, {"primaryProject": "x"}) is False


def test_hooks_install_remove_and_keep_others(tmp_path):
    path = tmp_path / "settings.json"
    theirs = {"type": "command", "command": "~/bin/notify.sh"}
    path.write_text(json.dumps({"model": "opus", "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [theirs]}],
        "Stop": [{"hooks": [theirs]}],
    }}))
    assert install_hooks(path, "/opt/vl/bin/vl hook") is True
    assert install_hooks(path, "/opt/vl/bin/vl hook") is False  # stable
    data = json.loads(path.read_text())
    assert data["model"] == "opus"
    assert data["hooks"]["Stop"] == [{"hooks": [theirs]}]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "Bash", "hooks": [theirs]}
    assert pre[1]["matcher"] == MATCHER
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "/opt/vl/bin/vl hook"
    assert claude.hooks_installed(path) == ["SessionStart", "UserPromptSubmit", "PreToolUse"]
    assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "/opt/vl/bin/vl hook"

    assert install_hooks(path, "'/Users/me/new place/vl' hook") is True  # moved: replaced, not added
    assert len(json.loads(path.read_text())["hooks"]["PreToolUse"]) == 2

    assert remove_hooks(path) is True
    data = json.loads(path.read_text())
    assert data["hooks"] == {"PreToolUse": [{"matcher": "Bash", "hooks": [theirs]}], "Stop": [{"hooks": [theirs]}]}
    assert claude.hooks_installed(path) == []


def test_is_our_hook():
    assert is_our_hook({"command": "vl hook"})
    assert is_our_hook({"command": "/Users/x/.local/bin/vl hook"})
    assert not is_our_hook({"command": "vl sync"})
    assert not is_our_hook({"command": "/bin/novl hook"})
    assert not is_our_hook({"command": "echo 'unclosed"})


def test_disables_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    repo = tmp_path / "repo"
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    claude.run(["git", "init", "-q", str(repo)])
    assert claude.disables_hooks(str(sub)) == []
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"disableAllHooks": true}')
    assert claude.disables_hooks(str(sub)) == [repo / ".claude" / "settings.json"]
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"disableAllHooks": true}')  # above the repo root
    assert claude.disables_hooks(str(sub)) == [repo / ".claude" / "settings.json"]
