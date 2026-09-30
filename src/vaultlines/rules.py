"""The access rules. Pure functions: config in, plan out.

Each folder writes to one vault W and lists the vaults it reads. A folder can:
  - read and write W
  - read each vault in `reads`, and write to them only after asking
  - not use any other vault

Folders that aren't listed use the "*" entry, which is set up at the user level,
so its vaults are available everywhere. Listed folders deny the ones they don't use.

Whether a folder may read what it lists is decided separately, in audience.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import STAR, Config

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
    writes: str
    reads: list[str]
    servers: dict[str, str]  # server name -> vault name
    ask: list[str]
    deny: list[str]


def folder_plan(cfg: Config, folder: str, drop_reads: bool = False) -> FolderPlan:
    """What a folder gets. `drop_reads` sets up only the write vault (for a refused "*")."""
    f = cfg.folders[folder]
    reads = [] if drop_reads else list(f.reads)
    servers = {server_name(n): n for n in [f.writes, *reads]}
    ask = [f"mcp__{server_name(n)}__{tool}" for n in reads for tool in WRITE_TOOLS]
    deny = []
    star = cfg.star
    if folder != STAR and star:
        for n in [star.writes, *star.reads]:
            if n not in servers.values():
                deny.append(f"mcp__{server_name(n)}")
    return FolderPlan(folder, f.writes, reads, servers, ask, deny)


def admin_denies(cfg: Config) -> list[str]:
    """Nobody should add or delete Basic Memory projects through Claude."""
    return [f"mcp__{server_name(n)}__{tool}" for n in sorted(cfg.vaults) for tool in ADMIN_TOOLS]
