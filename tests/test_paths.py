"""Everything vl owns lives in one folder: ~/.vaultlines (VAULTLINES_HOME in tests)."""

from pathlib import Path

from vaultlines import audience, config, hook, runtime, util


def test_the_default_home_is_dot_vaultlines(monkeypatch, tmp_path):
    monkeypatch.delenv("VAULTLINES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert util.vl_home() == tmp_path / ".vaultlines"


def test_every_path_comes_from_the_home(monkeypatch, tmp_path):
    root = tmp_path / "vl"
    monkeypatch.setenv("VAULTLINES_HOME", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))  # ignored now
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    assert util.vl_home() == root
    assert config.config_path() == root / "config.toml"
    assert util.state_dir() == root / "state"
    assert util.cache_dir() == root / "cache"
    assert util.vaults_dir() == root / "vaults"
    assert util.google_dir() == root / "google"
    assert util.fetch_dir("mixim-ai/vault-hq") == root / "cache" / "fetch" / "mixim-ai" / "vault-hq"
    assert runtime.path() == hook.runtime_path() == root / "state" / "runtime.json"
    assert audience.state_path() == root / "state" / "state.json"


def test_a_relative_home_is_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VAULTLINES_HOME", "vl")
    assert util.vl_home() == Path(tmp_path).resolve() / "vl"
