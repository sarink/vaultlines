from vaultlines import runtime
from vaultlines.config import Config, Vault


def test_build_stores_each_plugins_kind_prefixes_and_data(tmp_path):
    cfg = Config()
    cfg.vaults["personal"] = Vault("personal", tmp_path / "personal")
    cfg.plugins["basic-memory"] = {"kind": "basic-memory"}
    data = {"plugin": True, "projects": {"personal": "personal"}}
    out = runtime.build(cfg, {}, "sam", {"basic-memory": data})
    assert out["version"] == runtime.VERSION == 3
    assert out["plugins"] == {"basic-memory": {"kind": "basic-memory", "tool_prefixes": ["mcp__basic-memory__"],
                                               "data": data}}
    assert "basic_memory" not in out


def test_no_plugins(tmp_path):
    assert runtime.build(Config(), {}, "sam", {})["plugins"] == {}


def test_a_source_vault_gets_its_kind_and_fetch_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = Config()
    cfg.vaults["personal"] = Vault("personal", tmp_path / "personal")
    cfg.vaults["mixim-drive"] = Vault("mixim-drive", tmp_path / "mixim-drive")
    cfg.plugins["mixim-drive"] = {"kind": "drive", "vault": "mixim-drive", "remote": "vl-mixim-drive:"}
    out = runtime.build(cfg, {}, "sam", {"mixim-drive": {"remote": "vl-mixim-drive"}})
    assert out["vaults"]["mixim-drive"]["source"] == "drive"
    assert out["vaults"]["mixim-drive"]["fetch"] == [str((tmp_path / "cache").resolve() / "vaultlines" / "fetch" / "mixim-drive")]
    assert "source" not in out["vaults"]["personal"] and "fetch" not in out["vaults"]["personal"]
