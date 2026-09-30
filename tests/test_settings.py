import json

from vaultlines.claude import update_settings


def test_update_settings_keeps_other_rules(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text(json.dumps({
        "outputStyle": "Concise",
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["mcp__vl-old", "Bash(rm:*)"]},
    }))
    update_settings(path, primary="acme-founders", reads=["acme-everyone"],
                    ask=["mcp__vl-acme-everyone__write_note"], deny=["mcp__vl-personal"])
    data = json.loads(path.read_text())
    assert data["outputStyle"] == "Concise"
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]
    assert data["permissions"]["deny"] == ["Bash(rm:*)", "mcp__vl-personal"]
    assert data["permissions"]["ask"] == ["mcp__vl-acme-everyone__write_note"]
    assert data["basicMemory"]["primaryProject"] == "acme-founders"
    assert data["basicMemory"]["secondaryProjects"] == ["acme-everyone"]


def test_update_settings_unset_removes_only_ours(tmp_path):
    path = tmp_path / "settings.local.json"
    update_settings(path, primary="x", deny=["mcp__vl-personal"])
    update_settings(path, primary=None)
    assert json.loads(path.read_text()) == {}


def test_update_settings_is_stable(tmp_path):
    path = tmp_path / "s.json"
    assert update_settings(path, primary="x", deny=["mcp__vl-a"]) is True
    assert update_settings(path, primary="x", deny=["mcp__vl-a"]) is False
