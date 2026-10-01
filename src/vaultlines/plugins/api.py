"""What a plugin hands back to the hook. Standard library only: the hook imports it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Access:
    """Which vaults a tool call touches, as a plugin sees it. vl decides what's allowed."""
    kind: str = "read"  # "read" or "write"
    vaults: list[str] = field(default_factory=list)
    block: str | None = None  # the call can't be checked, so it's refused
    updated_input: dict | None = None


def here(folder: dict | None) -> str:
    """The vaults a folder entry (from runtime.json) uses, for messages."""
    if not folder:
        return "No vaults are set up for this folder."
    reads = folder.get("reads") or []
    return f"This folder uses {', '.join([folder['writes'], *reads])}."
