from pathlib import Path

from vaultlines.config import STAR, Config, Folder, Vault
from vaultlines.rules import admin_denies, folder_plan

WRITES = ("write_note", "edit_note", "delete_note", "move_note")


def cfg() -> Config:
    c = Config()
    for name in ("personal", "acme-founders", "acme-everyone", "globex"):
        c.vaults[name] = Vault(name, Path(f"/v/{name}"))
    c.folders[STAR] = Folder(STAR, "personal", [])
    c.folders["/legal"] = Folder("/legal", "acme-founders", ["acme-everyone"])
    c.folders["/app"] = Folder("/app", "acme-everyone", [])
    c.folders["/notes"] = Folder("/notes", "personal", ["acme-everyone"])
    return c


def test_folder_gets_its_write_and_read_vaults():
    plan = folder_plan(cfg(), "/legal")
    assert plan.servers == {"vl-acme-founders": "acme-founders", "vl-acme-everyone": "acme-everyone"}
    assert plan.writes == "acme-founders"
    assert plan.reads == ["acme-everyone"]


def test_writing_a_read_vault_asks_first():
    plan = folder_plan(cfg(), "/legal")
    assert plan.ask == [f"mcp__vl-acme-everyone__{t}" for t in WRITES]


def test_folder_denies_the_star_vault():
    assert folder_plan(cfg(), "/app").deny == ["mcp__vl-personal"]


def test_folder_writing_the_star_vault_does_not_deny_it():
    assert folder_plan(cfg(), "/notes").deny == []


def test_folder_reading_the_star_vault_does_not_deny_it():
    c = cfg()
    c.folders["/journal"] = Folder("/journal", "globex", ["personal"])
    assert folder_plan(c, "/journal").deny == []


def test_star_reads_are_denied_too():
    c = cfg()
    c.folders[STAR] = Folder(STAR, "personal", ["globex"])
    assert folder_plan(c, "/app").deny == ["mcp__vl-personal", "mcp__vl-globex"]


def test_star_plan_has_no_denies():
    c = cfg()
    c.folders[STAR] = Folder(STAR, "personal", ["acme-everyone"])
    plan = folder_plan(c, STAR)
    assert plan.servers == {"vl-personal": "personal", "vl-acme-everyone": "acme-everyone"}
    assert plan.deny == []
    assert len(plan.ask) == len(WRITES)


def test_drop_reads_keeps_only_the_write_vault():
    c = cfg()
    c.folders[STAR] = Folder(STAR, "personal", ["acme-everyone"])
    plan = folder_plan(c, STAR, drop_reads=True)
    assert plan.servers == {"vl-personal": "personal"}
    assert plan.ask == []


def test_admin_tools_denied_for_every_vault():
    denies = admin_denies(cfg())
    assert "mcp__vl-acme-founders__delete_project" in denies
    assert "mcp__vl-personal__create_memory_project" in denies
    assert len(denies) == 2 * len(cfg().vaults)
