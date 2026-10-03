"""config.toml: your own changes. vl works without any of them."""

import pytest

from vaultlines import config
from vaultlines.util import VlError

GOOD = """
sync_interval  = 300
on_leak        = "block"

[repos."acme/website"]
writes    = "acme/vault-public"
reads     = ["kabir/vault-recipes"]
auto_pull = true

[repos."acme/*"]
reads = ["kabir/vault-side"]

[folders."~/Documents/writing"]
writes = "kabir/vault-recipes"

[folders."{folder}"]
reads = ["kabir/vault-recipes"]
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path / "vl"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def write(home, text):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("{folder}", str(home / "notes")))
    return path


def test_no_file_is_the_defaults(home):
    cfg = config.load_file()
    assert (cfg.sync_interval, cfg.check_interval, cfg.on_leak, cfg.basic_memory) == (600, 86400, "ask", True)
    assert (cfg.repos, cfg.folders) == ({}, {})


def test_the_template_is_only_comments_and_loads_as_the_defaults(home):
    config.write_template()
    text = config.config_path().read_text()
    assert all(line.startswith("#") or not line.strip() for line in text.splitlines())
    assert '# [repos."acme/website"]' in text
    cfg = config.load_file()
    assert (cfg.repos, cfg.folders) == ({}, {})
    config.write_template()  # never replaces your file
    assert config.config_path().read_text() == text


def test_uncommenting_the_template_examples_gives_a_good_config(home):
    config.write_template()
    path = config.config_path()
    text = path.read_text()
    # Uncomment every example line (the "# " ones that hold TOML), the way a person would.
    lines = []
    for line in text.splitlines():
        body = line[2:] if line.startswith("# ") else None
        if body and (body.startswith(("[", "sync_interval", "check_interval", "on_leak", "basic_memory", "writes",
                                      "reads", "auto_pull"))):
            lines.append(body)
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n")
    cfg = config.load_file()
    assert cfg.repos["acme/website"].writes == "acme/vault-public"
    assert cfg.folders[str(home / "Documents" / "writing")].writes == "kabir/vault-recipes"


def test_load_a_good_config(home):
    write(home, GOOD)
    cfg = config.load_file()
    assert (cfg.sync_interval, cfg.on_leak) == (300, "block")
    website = cfg.repos["acme/website"]
    assert (website.writes, website.reads, website.auto_pull) == ("acme/vault-public", ["kabir/vault-recipes"], True)
    assert cfg.repos["acme/*"].writes is None
    writing = cfg.folders[str(home / "Documents" / "writing")]
    assert (writing.writes, writing.reads) == ("kabir/vault-recipes", [])
    assert cfg.folders[str(home / "notes")].writes is None


def test_repo_names_ignore_case(home):
    write(home, '[repos."ACME/Website"]\nwrites = "ACME/vault-public"\n')
    assert config.load_file().repos["acme/website"].writes == "acme/vault-public"


@pytest.mark.parametrize("old, new, message", [
    ('on_leak        = "block"', 'on_leak = "warn"', "on_leak"),
    ("sync_interval  = 300", "sync_interval = 0", "sync_interval"),
    ("sync_interval  = 300", "sync_interval = true", "sync_interval"),
    ("sync_interval  = 300", "colour = 1", "colour: unknown key"),
    ("sync_interval  = 300", "basic_memory = 1", "basic_memory: should be true or false"),
    ("sync_interval  = 300", '[vaults."kabir/vault-recipes"]', "vaults: unknown key"),
    ('[repos."acme/website"]', '[repos."website"]', "OWNER/REPO"),
    ('[repos."acme/*"]', '[repos."*/*"]', "OWNER/REPO"),
    ('writes    = "acme/vault-public"', 'writes = "vault-public"', "OWNER/REPO"),
    ('writes    = "acme/vault-public"', 'writes = ["acme/vault-public"]', "should be one vault"),
    ('reads     = ["kabir/vault-recipes"]', 'reads = "kabir/vault-recipes"', "should be a list"),
    ('reads     = ["kabir/vault-recipes"]', 'reads = ["acme/vault-public"]', "can't also be in reads"),
    ("auto_pull = true", "autopull = true", "unknown key"),
    ("auto_pull = true", 'dangerously_skip_hook_guards = "yes"', "dangerously_skip_hook_guards: should be true or false"),
    ("auto_pull = true", "allow_vl_commands = true", "allow_vl_commands: unknown key"),
    ("auto_pull = true", 'auto_pull = "yes"', "true or false"),
    ('reads = ["kabir/vault-side"]', 'auto_pull = true', "auto_pull needs one repo"),
    ('[folders."~/Documents/writing"]', '[folders."*"]', "a folder"),
    ('[folders."~/Documents/writing"]', '[folders."~/Documents/writing"]\nauto_pull = true', "unknown key"),
])
def test_validation_errors(home, old, new, message):
    text = GOOD.replace(old, new, 1)
    assert text != GOOD
    write(home, text)
    with pytest.raises(VlError) as e:
        config.load_file()
    assert message in str(e.value)
    assert "config.toml" in str(e.value)


def test_the_same_folder_twice(home):
    write(home, GOOD + '\n[folders."~/Documents/writing/"]\nwrites = "kabir/vault-recipes"\n')
    with pytest.raises(VlError, match="same folder"):
        config.load_file()


def test_basic_memory_can_be_switched_off(home):
    config.write_template(basic_memory=False)
    assert config.load_file().basic_memory is False
    assert "basic_memory = false" in config.config_path().read_text()


def test_dev_mode_for_a_repo(home):
    write(home, '[repos."sarink/vaultlines"]\ndangerously_skip_hook_guards = true\n')
    assert config.load_file().repos["sarink/vaultlines"].dangerously_skip_hook_guards is True
