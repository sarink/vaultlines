from vaultlines import runtime
from vaultlines.config import Config, Vault


def test_build_stores_each_plugins_kind_prefixes_and_data(tmp_path):
    cfg = Config()
    cfg.vaults["personal"] = Vault("personal", tmp_path / "personal")
    cfg.plugins["basic-memory"] = {"kind": "basic-memory"}
    data = {"plugin": True, "projects": {"personal": "personal"}}
    out = runtime.build(cfg, {}, "sam", {"basic-memory": data})
    assert out["version"] == runtime.VERSION == 2
    assert out["plugins"] == {"basic-memory": {"kind": "basic-memory", "tool_prefixes": ["mcp__basic-memory__"],
                                               "data": data}}
    assert "basic_memory" not in out


def test_no_plugins(tmp_path):
    assert runtime.build(Config(), {}, "sam", {})["plugins"] == {}
