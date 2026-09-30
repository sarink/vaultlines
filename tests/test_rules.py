from pathlib import Path

from vaultlines.config import Config, Vault
from vaultlines.rules import admin_denies, folder_plan, readable

WRITES = ("write_note", "edit_note", "delete_note", "move_note")


def cfg() -> Config:
    c = Config(default_vault="personal")
    for name, level, team in [
        ("personal", "personal", None),
        ("side-project", "personal", None),
        ("acme-private", "private", "acme"),
        ("acme-public", "public", "acme"),
        ("other-public", "public", "other"),
    ]:
        c.vaults[name] = Vault(name, Path(f"/v/{name}"), level, team)
    return c


def test_personal_reads_every_public_vault():
    assert readable(cfg(), "side-project") == ["acme-public", "other-public"]


def test_private_reads_only_its_teams_public_vault():
    assert readable(cfg(), "acme-private") == ["acme-public"]


def test_public_reads_nothing_else():
    assert readable(cfg(), "acme-public") == []


def test_private_folder_plan():
    plan = folder_plan(cfg(), "/f", "acme-private")
    assert plan.servers == {"vl-acme-private": "acme-private", "vl-acme-public": "acme-public"}
    assert plan.ask == [f"mcp__vl-acme-public__{t}" for t in WRITES]
    assert plan.deny == ["mcp__vl-personal"]


def test_public_folder_blocks_personal_and_sees_nothing_else():
    plan = folder_plan(cfg(), "/f", "acme-public")
    assert plan.servers == {"vl-acme-public": "acme-public"}
    assert plan.ask == []
    assert plan.deny == ["mcp__vl-personal"]


def test_folder_bound_to_default_vault_does_not_deny_it():
    plan = folder_plan(cfg(), "/f", "personal")
    assert "vl-personal" in plan.servers
    assert plan.deny == []


def test_admin_tools_denied_for_every_vault():
    denies = admin_denies(cfg())
    assert "mcp__vl-acme-private__delete_project" in denies
    assert "mcp__vl-personal__create_memory_project" in denies
    assert len(denies) == 2 * len(cfg().vaults)
