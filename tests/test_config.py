import pytest

from vaultlines import config
from vaultlines.config import STAR, Config, Folder, Vault
from vaultlines.util import VlError

GOOD = """
[settings]
sync_interval = 600

[vaults.personal]
path = "~/Vaults/personal"
remote = "https://github.com/sam/vault-personal.git"

[vaults.acme-everyone]
path = "~/Vaults/acme-everyone"
remote = "https://github.com/acme/acme-everyone"

[vaults.side]
path = "~/Vaults/side"

[folders."*"]
writes = "personal"
reads = []

[folders."{folder}"]
writes = "side"
reads = ["acme-everyone"]
auto_pull = true
"""


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("VAULTLINES_CONFIG", str(path))
    return path


def write(path, text, folder):
    path.write_text(text.replace("{folder}", str(folder)))


def test_load_good_config(cfg_file, tmp_path):
    write(cfg_file, GOOD, tmp_path)
    cfg = config.load()
    assert cfg.vaults["side"].remote is None
    assert cfg.star == Folder(STAR, "personal", [])
    f = cfg.folders[str(tmp_path.resolve())]
    assert (f.writes, f.reads, f.auto_pull) == ("side", ["acme-everyone"], True)


def test_round_trip(cfg_file, tmp_path):
    write(cfg_file, GOOD, tmp_path)
    cfg = config.load()
    config.save(cfg)
    again = config.load()
    assert again.vaults == cfg.vaults
    assert again.folders == cfg.folders
    assert '[folders."*"]' in cfg_file.read_text()




@pytest.mark.parametrize("old, new, message", [
    ('writes = "side"\n', "", ".writes: missing"),
    ('reads = ["acme-everyone"]\n', "", ".reads: missing"),
    ('writes = "side"', 'writes = "nope"', "no vault named 'nope'"),
    ('reads = ["acme-everyone"]', 'reads = ["nope"]', "no vault named 'nope'"),
    ('reads = ["acme-everyone"]', 'reads = ["side"]', "can't also be in reads"),
    ('[folders."*"]\nwrites = "personal"\nreads = []',
     '[folders."*"]\nwrites = "personal"\nreads = []\nauto_pull = true', 'folders."*".auto_pull'),
    ('remote = "https://github.com/acme/acme-everyone"', 'remote = "git@github.com:acme/x.git"', "must be GitHub URLs"),
    ('remote = "https://github.com/acme/acme-everyone"', 'remote = "https://gitlab.com/acme/x.git"', "must be GitHub URLs"),
    ("auto_pull = true", "autopull = true", "unknown key"),
    ('[vaults.side]', '[vaults.Side]', "lowercase"),
    ('[folders."*"]\nwrites = "personal"\nreads = []',
     '[folders."*"]\nwrites = "personal"\nreads = ["side"]', "asks first in every folder"),
])
def test_validation_errors(cfg_file, tmp_path, old, new, message):
    text = GOOD.replace(old, new, 1)
    assert text != GOOD
    write(cfg_file, text, tmp_path)
    with pytest.raises(VlError) as e:
        config.load()
    assert message in str(e.value)
    assert "config.toml" in str(e.value)


def test_same_folder_twice(cfg_file, tmp_path):
    write(cfg_file, GOOD + f'\n[folders."{tmp_path}/"]\nwrites = "side"\nreads = []\n', tmp_path)
    with pytest.raises(VlError, match="same folder"):
        config.load()


def test_file_remotes_only_in_tests(cfg_file, tmp_path, monkeypatch):
    write(cfg_file, GOOD.replace("https://github.com/acme/acme-everyone", "file:///tmp/x.git"), tmp_path)
    with pytest.raises(VlError):
        config.load()
    monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
    assert config.load().vaults["acme-everyone"].remote == "file:///tmp/x.git"


def test_save_refuses_invalid(cfg_file, tmp_path):
    cfg = Config()
    cfg.vaults["personal"] = Vault("personal", tmp_path)
    cfg.folders[STAR] = Folder(STAR, "missing", [])
    with pytest.raises(VlError):
        config.save(cfg)
    assert not cfg_file.exists()
