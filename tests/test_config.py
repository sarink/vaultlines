import pytest

from vaultlines import config
from vaultlines.config import Config, Folder, Vault, folder_for
from vaultlines.util import VlError

GOOD = """
[settings]
sync_interval  = 600
check_interval = 86400
on_leak        = "ask"

[vaults.personal]
path   = "~/Vaults/personal"
remote = "https://github.com/sam/vault-personal.git"

[vaults.acme-everyone]
path   = "~/Vaults/acme-everyone"
remote = "https://github.com/acme/acme-everyone"

[vaults.side]
path   = "~/Vaults/side"

[folders."~"]
writes = "personal"

[folders."{folder}"]
writes    = "side"
reads     = ["acme-everyone"]
auto_pull = true

[plugins.basic-memory]
kind = "basic-memory"
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
    assert cfg.on_leak == "ask"
    assert cfg.plugins == {"basic-memory": {"kind": "basic-memory"}}
    f = cfg.folders[str(tmp_path.resolve())]
    assert (f.writes, f.reads, f.auto_pull) == ("side", ["acme-everyone"], True)
    home = cfg.folders[str(config.expand("~"))]
    assert home.reads == []  # reads is optional


def test_round_trip(cfg_file, tmp_path):
    write(cfg_file, GOOD, tmp_path)
    cfg = config.load()
    config.save(cfg)
    again = config.load()
    assert again.vaults == cfg.vaults
    assert again.folders == cfg.folders
    assert again.plugins == {"basic-memory": {"kind": "basic-memory"}}
    text = cfg_file.read_text()
    assert '[folders."~"]' in text
    assert '[plugins.basic-memory]\nkind = "basic-memory"' in text


def test_plugin_absent_is_off(cfg_file, tmp_path):
    write(cfg_file, GOOD.replace('[plugins.basic-memory]\nkind = "basic-memory"\n', ""), tmp_path)
    assert config.load().plugins == {}


def test_plugin_name_is_free(cfg_file, tmp_path):
    write(cfg_file, GOOD.replace("[plugins.basic-memory]", "[plugins.memory]"), tmp_path)
    assert config.load().plugins == {"memory": {"kind": "basic-memory"}}


@pytest.mark.parametrize("old, new, message", [
    ('writes    = "side"\n', "", ".writes: missing"),
    ('writes    = "side"', 'writes = "nope"', "no vault named 'nope'"),
    ('reads     = ["acme-everyone"]', 'reads = ["nope"]', "no vault named 'nope'"),
    ('reads     = ["acme-everyone"]', 'reads = ["side"]', "can't also be in reads"),
    ('reads     = ["acme-everyone"]', 'reads = "acme-everyone"', "list of vault names"),
    ('[folders."~"]', '[folders."*"]', 'use "~"'),
    ('on_leak        = "ask"', 'on_leak = "warn"', "on_leak"),
    ("check_interval = 86400", "check_interval = 0", "check_interval"),
    ('remote = "https://github.com/acme/acme-everyone"', 'remote = "git@github.com:acme/x.git"', "must be GitHub URLs"),
    ('remote = "https://github.com/acme/acme-everyone"', 'remote = "https://gitlab.com/acme/x.git"', "must be GitHub URLs"),
    ("auto_pull = true", "autopull = true", "unknown key"),
    ("[vaults.side]", "[vaults.Side]", "lowercase"),
    ('kind = "basic-memory"\n', "", "plugins.basic-memory.kind: missing"),
    ('kind = "basic-memory"', 'kind = "other-memory"', "unknown kind"),
    ('kind = "basic-memory"', 'kind = "basic-memory"\nservers = "x"', "plugins.basic-memory.servers: unknown key"),
    ('kind = "basic-memory"', 'kind = "basic-memory"\ncommand = ""', "plugins.basic-memory.command: should be a command"),
    ('kind = "basic-memory"', 'kind = "basic-memory"\n[plugins.again]\nkind = "basic-memory"', "only one"),
    ("[plugins.basic-memory]", "[adapters.basic-memory]", "adapters: unknown key"),
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
    write(cfg_file, GOOD + f'\n[folders."{tmp_path}/"]\nwrites = "side"\n', tmp_path)
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
    cfg.folders["/x"] = Folder("/x", "missing")
    with pytest.raises(VlError):
        config.save(cfg)
    assert not cfg_file.exists()


def test_folder_for_uses_the_closest_listed_parent(tmp_path):
    cfg = Config()
    home = config.expand("~")
    for path, writes in ((str(home), "personal"), (str(tmp_path / "acme"), "mp"),
                         (str(tmp_path / "acme" / "site"), "pub")):
        cfg.folders[path] = Folder(path, writes)
    (tmp_path / "acme" / "site" / "sub").mkdir(parents=True)
    (tmp_path / "acme-other").mkdir()
    assert folder_for(cfg, tmp_path / "acme" / "site" / "sub").writes == "pub"
    assert folder_for(cfg, tmp_path / "acme").writes == "mp"
    assert folder_for(cfg, tmp_path / "acme-other") is None or folder_for(cfg, tmp_path / "acme-other").writes != "mp"
    assert folder_for(cfg, home / "anything").writes == "personal"
    assert folder_for(cfg, "/") is None


DRIVE = GOOD + """
[vaults.drive]
path = "~/Vaults/drive"

[plugins.mixim-drive]
kind   = "drive"
vault  = "drive"
remote = "mixim:"
"""


def test_drive_plugin_loads(cfg_file, tmp_path):
    write(cfg_file, DRIVE, tmp_path)
    cfg = config.load()
    assert cfg.plugins["mixim-drive"] == {"kind": "drive", "vault": "drive", "remote": "mixim:"}
    config.save(cfg)
    assert config.load().plugins == cfg.plugins


def test_a_source_vault_can_be_read(cfg_file, tmp_path):
    write(cfg_file, DRIVE.replace('reads     = ["acme-everyone"]', 'reads = ["acme-everyone", "drive"]'), tmp_path)
    assert config.load().folders[str(tmp_path.resolve())].reads == ["acme-everyone", "drive"]


@pytest.mark.parametrize("old, new, message", [
    ('vault  = "drive"\n', "", "plugins.mixim-drive.vault: missing"),
    ('vault  = "drive"', 'vault = "nope"', "plugins.mixim-drive.vault: no vault named 'nope'"),
    ('vault  = "drive"', 'vault = 5', "plugins.mixim-drive.vault: should be a vault name"),
    ('writes    = "side"', 'writes = "drive"', "writes: 'drive' is filled by plugin 'mixim-drive'"),
    ('remote = "mixim:"\n', 'remote = "mixim:"\n[plugins.second]\nkind = "drive"\nvault = "drive"\nremote = "x:"\n',
     "plugins.second.vault: 'drive' is already filled by plugin 'mixim-drive'"),
    ('remote = "mixim:"', 'remote = "mixim:"\nevery = 0', "plugins.mixim-drive.every: should be a whole number"),
    ('remote = "mixim:"', 'remote = "mixim:"\nevery = "1h"', "plugins.mixim-drive.every: should be a whole number"),
    ('remote = "mixim:"', 'remote = "mixim:"\nmax_size = 50', "plugins.mixim-drive.max_size"),
    ('remote = "mixim:"\n', "", "plugins.mixim-drive.remote: missing"),
    ('remote = "mixim:"', 'remote = "mixim:"\ncommand = "rclone --config /x"', "plugins.mixim-drive.command: unknown key"),
    ('remote = "mixim:"', 'remote = "mixim,service_account_file=/k.json:"',
     "plugins.mixim-drive.remote: rclone settings in a remote can't include service_account_file"),
    ('kind = "basic-memory"', 'kind = "basic-memory"\nvault = "drive"', "plugins.basic-memory.vault: unknown key"),
    ('kind = "basic-memory"', 'kind = "basic-memory"\nevery = 60', "plugins.basic-memory.every: unknown key"),
])
def test_source_errors(cfg_file, tmp_path, old, new, message):
    text = DRIVE.replace(old, new, 1)
    assert text != DRIVE
    write(cfg_file, text, tmp_path)
    with pytest.raises(VlError) as e:
        config.load()
    assert message in str(e.value)
