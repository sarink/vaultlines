"""The access rules. Pure functions: config in, plan out.

A folder bound to a vault can:
  - read and write its own vault
  - read some other vaults, and write to them only after asking:
      personal vault -> every public vault
      private vault  -> the public vaults of the same team
      public vault   -> nothing else
  - never use the default personal vault (unless it is the bound vault)

This is "no read up, no write down": a folder never sees a vault that is more
private than its own, and moving information into a less private vault always
goes through a person.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config

PREFIX = "vl-"
WRITE_TOOLS = ("write_note", "edit_note", "delete_note", "move_note")
ADMIN_TOOLS = ("create_memory_project", "delete_project")


def server_name(vault: str) -> str:
    return PREFIX + vault


def is_managed_rule(rule: str) -> bool:
    return rule.startswith(f"mcp__{PREFIX}")


@dataclass
class FolderPlan:
    folder: str
    vault: str
    servers: dict[str, str]  # server name -> vault name
    reads: list[str]  # other vaults this folder can read
    ask: list[str]
    deny: list[str]


def readable(cfg: Config, vault: str) -> list[str]:
    v = cfg.vault(vault)
    others = [o for o in cfg.vaults.values() if o.name != vault and o.level == "public"]
    if v.level == "personal":
        return sorted(o.name for o in others)
    if v.level == "private":
        return sorted(o.name for o in others if o.team and o.team == v.team)
    return []


def folder_plan(cfg: Config, folder: str, vault: str) -> FolderPlan:
    reads = readable(cfg, vault)
    servers = {server_name(n): n for n in [vault, *reads]}
    ask = [f"mcp__{server_name(n)}__{tool}" for n in reads for tool in WRITE_TOOLS]
    deny = []
    default = cfg.default_vault
    if default and default != vault and default not in reads:
        deny.append(f"mcp__{server_name(default)}")
    return FolderPlan(folder, vault, servers, reads, ask, deny)


def admin_denies(cfg: Config) -> list[str]:
    """Nobody should add or delete Basic Memory projects through Claude."""
    return [f"mcp__{server_name(n)}__{tool}" for n in sorted(cfg.vaults) for tool in ADMIN_TOOLS]
