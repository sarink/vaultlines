import json

from vaultlines import config
from vaultlines.claude import update_settings
from vaultlines.config import Config, Vault


def test_update_settings_keeps_other_rules(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text(json.dumps({
        "outputStyle": "Concise",
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["mcp__vl-old", "Bash(rm:*)"]},
    }))
    update_settings(path, primary="acme-private", reads=["acme-public"],
                    ask=["mcp__vl-acme-public__write_note"], deny=["mcp__vl-personal"])
    data = json.loads(path.read_text())
    assert data["outputStyle"] == "Concise"
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]
    assert data["permissions"]["deny"] == ["Bash(rm:*)", "mcp__vl-personal"]
    assert data["permissions"]["ask"] == ["mcp__vl-acme-public__write_note"]
    assert data["basicMemory"]["primaryProject"] == "acme-private"
    assert data["basicMemory"]["secondaryProjects"] == ["acme-public"]


def test_update_settings_unbind_removes_only_ours(tmp_path):
    path = tmp_path / "settings.local.json"
    update_settings(path, primary="x", deny=["mcp__vl-personal"])
    update_settings(path, primary=None)
    assert json.loads(path.read_text()) == {}


def test_update_settings_is_stable(tmp_path):
    path = tmp_path / "s.json"
    assert update_settings(path, primary="x", deny=["mcp__vl-a"]) is True
    assert update_settings(path, primary="x", deny=["mcp__vl-a"]) is False


def test_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTLINES_CONFIG", str(tmp_path / "config.toml"))
    c = Config(default_vault="personal", github_owner="someone")
    c.vaults["personal"] = Vault("personal", tmp_path / "p", "personal")
    c.vaults["acme-public"] = Vault("acme-public", tmp_path / "a", "public", "acme", "https://x/y.git")
    c.bindings[str(tmp_path)] = "acme-public"
    c.follow.append(str(tmp_path))
    config.save(c)
    loaded = config.load()
    assert loaded.vaults["acme-public"].team == "acme"
    assert loaded.vaults["acme-public"].remote == "https://x/y.git"
    assert loaded.bindings == {str(tmp_path.resolve()): "acme-public"}
    assert loaded.default_vault == "personal"
