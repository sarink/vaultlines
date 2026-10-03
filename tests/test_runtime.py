from pathlib import Path

from vaultlines import runtime
from vaultlines.audience import Audience
from vaultlines.config import Config, Rule
from vaultlines.vaults import Info, Vault


def make(tmp_path, monkeypatch) -> Config:
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path / "vl"))
    cfg = Config(me="kabir", owners=["kabir", "acme"])

    def add(vid, about="", notes_from=(), source=None, remote=True):
        path = tmp_path / "vl" / "vaults" / vid
        path.mkdir(parents=True)
        cfg.vaults[vid] = Vault(vid, path, f"https://github.com/{vid}.git" if remote else None,
                                Info(about, list(notes_from), source))

    add("kabir/vault-kabir-personal", "Kabir's personal notes.", remote=False)
    add("acme/vault-public", "Everyone.", ["acme/marketing", "acme/studio", "kabir/blog"])
    add("acme/vault-private", "Founders.", ["acme/legal-case", "acme/studio"])
    add("acme/vault-hq", "Drive.", ["acme/hq-notes"], source={"kind": "gdrive"})
    add("acme/vault-kabir-personal", remote=False)
    add("kabir/vault-recipes", remote=False)
    return cfg


def test_build_v5(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    cfg.repos["acme/website"] = Rule("acme/website", "acme/vault-public", ["kabir/vault-recipes"], True)
    cfg.folders[str(tmp_path / "writing")] = Rule(str(tmp_path / "writing"), "kabir/vault-recipes", [])
    warnings = []
    auds = {"acme/vault-public": Audience("people", ("kabir", "ana"))}
    data = {"basic-memory": {"plugin": True, "projects": {}}}
    out = runtime.build(cfg, auds, data, warnings=warnings)
    assert out["version"] == runtime.VERSION == 5
    assert out["me"] == "kabir"
    assert sorted(out["vaults"]) == ["acme/vault-hq", "acme/vault-kabir-personal", "acme/vault-private", "acme/vault-public",
                                     "kabir/vault-kabir-personal", "kabir/vault-recipes"]
    public = out["vaults"]["acme/vault-public"]
    assert "id" not in public and public["about"] == "Everyone."
    assert public["paths"] == [str((tmp_path / "vl" / "vaults" / "acme" / "vault-public").resolve())]
    assert public["audience"] == {"kind": "people", "logins": ["kabir", "ana"], "reason": ""}
    assert out["vaults"]["acme/vault-private"]["audience"]["reason"] == "not checked yet"
    hq = out["vaults"]["acme/vault-hq"]
    assert hq["source"] == "gdrive"
    assert hq["fetch"] == [str((tmp_path / "vl").resolve() / "cache" / "fetch" / "acme" / "vault-hq")]
    assert "source" not in public

    acme = out["owners"]["acme"]
    assert acme["personal"] == "acme/vault-kabir-personal"
    assert acme["vaults"] == ["acme/vault-hq", "acme/vault-kabir-personal", "acme/vault-private", "acme/vault-public"]
    assert acme["notes_from"] == {"acme/legal-case": "acme/vault-private", "acme/marketing": "acme/vault-public"}
    assert acme["conflicts"] == {"acme/studio": ["acme/vault-private", "acme/vault-public"]}
    assert out["owners"]["kabir"] == {"personal": "kabir/vault-kabir-personal", "vaults": ["kabir/vault-kabir-personal", "kabir/vault-recipes"],
                                      "notes_from": {}, "conflicts": {}}
    assert out["repos"] == {"acme/website": {"writes": "acme/vault-public", "reads": ["kabir/vault-recipes"]}}
    assert out["folders"] == {str(tmp_path / "writing"): {"writes": "kabir/vault-recipes", "reads": []}}
    assert out["default"] == {"writes": "kabir/vault-kabir-personal", "reads": []}
    assert out["plugins"]["basic-memory"]["tool_prefixes"] == ["mcp__basic-memory__"]
    assert any("kabir/blog, which belongs to another owner" in w for w in warnings)
    assert any("acme/vault-hq: notes_from is ignored" in w for w in warnings)


def test_config_entries_for_vaults_that_arent_here_are_left_out(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    cfg.repos["acme/x"] = Rule("acme/x", "acme/vault-gone", ["acme/vault-hq", "nobody/vault-y"])
    cfg.repos["acme/y"] = Rule("acme/y", "acme/vault-hq")
    warnings = []
    out = runtime.build(cfg, {}, {}, warnings=warnings)
    assert out["repos"]["acme/x"] == {"writes": None, "reads": ["acme/vault-hq"]}
    assert out["repos"]["acme/y"] == {"writes": None, "reads": []}
    assert any("no vault acme/vault-gone" in w for w in warnings)
    assert any("acme/vault-hq comes from gdrive, so notes can't be saved there" in w for w in warnings)


def test_lost_vaults_stay_known_but_arent_used(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    out = runtime.build(cfg, {}, {}, lost={"acme/vault-private"})
    assert out["vaults"]["acme/vault-private"]["lost"] is True
    assert "acme/vault-private" not in out["owners"]["acme"]["vaults"]
    assert out["owners"]["acme"]["notes_from"] == {"acme/marketing": "acme/vault-public",
                                                       "acme/studio": "acme/vault-public"}


def test_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path))
    assert runtime.stale(None) == "runtime.json is missing"
    assert "another version" in runtime.stale({"version": 3})
    Path(tmp_path / "config.toml").write_text("")
    assert "config.toml changed" in runtime.stale({"version": runtime.VERSION, "written_at": 0})


def test_dev_mode_is_in_the_repos_entry(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    cfg.repos["kabir/vaultlines"] = Rule("kabir/vaultlines", dangerously_skip_hook_guards=True)
    out = runtime.build(cfg, {}, {})
    assert out["repos"]["kabir/vaultlines"] == {"writes": None, "reads": [], "dangerously_skip_hook_guards": True}
    cfg.repos["kabir/vaultlines"] = Rule("kabir/vaultlines")
    assert runtime.build(cfg, {}, {})["repos"]["kabir/vaultlines"] == {"writes": None, "reads": []}
