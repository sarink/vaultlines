from pathlib import Path

from vaultlines import runtime
from vaultlines.audience import Audience
from vaultlines.config import Config, Rule
from vaultlines.vaults import Info, Vault


def make(tmp_path, monkeypatch) -> Config:
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path / "vl"))
    cfg = Config(me="kabir", owners=["kabir", "mixim-ai"])

    def add(vid, about="", notes_from=(), source=None, remote=True):
        path = tmp_path / "vl" / "vaults" / vid
        path.mkdir(parents=True)
        cfg.vaults[vid] = Vault(vid, path, f"https://github.com/{vid}.git" if remote else None,
                                Info(about, list(notes_from), source))

    add("kabir/vault-kabir-personal", "Kabir's personal notes.", remote=False)
    add("mixim-ai/vault-public", "Everyone.", ["mixim-ai/marketing", "mixim-ai/studio", "kabir/blog"])
    add("mixim-ai/vault-private", "Founders.", ["mixim-ai/jorge-ip-theft", "mixim-ai/studio"])
    add("mixim-ai/vault-hq", "Drive.", ["mixim-ai/hq-notes"], source={"kind": "gdrive"})
    add("mixim-ai/vault-kabir-personal", remote=False)
    add("kabir/vault-recipes", remote=False)
    return cfg


def test_build_v4(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    cfg.repos["mixim-ai/postal"] = Rule("mixim-ai/postal", "mixim-ai/vault-public", ["kabir/vault-recipes"], True)
    cfg.folders[str(tmp_path / "writing")] = Rule(str(tmp_path / "writing"), "kabir/vault-recipes", [])
    warnings = []
    auds = {"mixim-ai/vault-public": Audience("people", ("kabir", "ana"))}
    data = {"basic-memory": {"plugin": True, "projects": {}}}
    out = runtime.build(cfg, auds, data, warnings=warnings)
    assert out["version"] == runtime.VERSION == 4
    assert out["me"] == "kabir"
    assert sorted(out["vaults"]) == ["kabir-personal", "kabir-recipes", "mixim-ai-hq", "mixim-ai-kabir-personal",
                                     "mixim-ai-private", "mixim-ai-public"]
    public = out["vaults"]["mixim-ai-public"]
    assert public["id"] == "mixim-ai/vault-public" and public["about"] == "Everyone."
    assert public["paths"] == [str((tmp_path / "vl" / "vaults" / "mixim-ai" / "vault-public").resolve())]
    assert public["audience"] == {"kind": "people", "logins": ["kabir", "ana"], "reason": ""}
    assert out["vaults"]["mixim-ai-private"]["audience"]["reason"] == "not checked yet"
    hq = out["vaults"]["mixim-ai-hq"]
    assert hq["source"] == "gdrive"
    assert hq["fetch"] == [str((tmp_path / "vl").resolve() / "cache" / "fetch" / "mixim-ai" / "vault-hq")]
    assert "source" not in public

    mixim = out["owners"]["mixim-ai"]
    assert mixim["personal"] == "mixim-ai-kabir-personal"
    assert mixim["vaults"] == ["mixim-ai-hq", "mixim-ai-kabir-personal", "mixim-ai-private", "mixim-ai-public"]
    assert mixim["notes_from"] == {"mixim-ai/jorge-ip-theft": "mixim-ai-private", "mixim-ai/marketing": "mixim-ai-public"}
    assert mixim["conflicts"] == {"mixim-ai/studio": ["mixim-ai-private", "mixim-ai-public"]}
    assert out["owners"]["kabir"] == {"personal": "kabir-personal", "vaults": ["kabir-personal", "kabir-recipes"],
                                      "notes_from": {}, "conflicts": {}}
    assert out["repos"] == {"mixim-ai/postal": {"writes": "mixim-ai-public", "reads": ["kabir-recipes"]}}
    assert out["folders"] == {str(tmp_path / "writing"): {"writes": "kabir-recipes", "reads": []}}
    assert out["default"] == {"writes": "kabir-personal", "reads": []}
    assert out["plugins"]["basic-memory"]["tool_prefixes"] == ["mcp__basic-memory__"]
    assert any("kabir/blog, which belongs to another owner" in w for w in warnings)
    assert any("mixim-ai/vault-hq: notes_from is ignored" in w for w in warnings)


def test_config_entries_for_vaults_that_arent_here_are_left_out(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    cfg.repos["mixim-ai/x"] = Rule("mixim-ai/x", "mixim-ai/vault-gone", ["mixim-ai/vault-hq", "nobody/vault-y"])
    cfg.repos["mixim-ai/y"] = Rule("mixim-ai/y", "mixim-ai/vault-hq")
    warnings = []
    out = runtime.build(cfg, {}, {}, warnings=warnings)
    assert out["repos"]["mixim-ai/x"] == {"writes": None, "reads": ["mixim-ai-hq"]}
    assert out["repos"]["mixim-ai/y"] == {"writes": None, "reads": []}
    assert any("no vault mixim-ai/vault-gone" in w for w in warnings)
    assert any("mixim-ai/vault-hq comes from gdrive, so notes can't be saved there" in w for w in warnings)


def test_lost_vaults_stay_known_but_arent_used(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    out = runtime.build(cfg, {}, {}, lost={"mixim-ai/vault-private"})
    assert out["vaults"]["mixim-ai-private"]["lost"] is True
    assert "mixim-ai-private" not in out["owners"]["mixim-ai"]["vaults"]
    assert out["owners"]["mixim-ai"]["notes_from"] == {"mixim-ai/marketing": "mixim-ai-public",
                                                       "mixim-ai/studio": "mixim-ai-public"}


def test_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path))
    assert runtime.stale(None) == "runtime.json is missing"
    assert "another version" in runtime.stale({"version": 3})
    Path(tmp_path / "config.toml").write_text("")
    assert "config.toml changed" in runtime.stale({"version": runtime.VERSION, "written_at": 0})


def test_allow_vl_commands_is_in_the_repos_entry(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    cfg.repos["kabir/vaultlines"] = Rule("kabir/vaultlines", allow_vl_commands=True)
    out = runtime.build(cfg, {}, {})
    assert out["repos"]["kabir/vaultlines"] == {"writes": None, "reads": [], "allow_vl_commands": True}
