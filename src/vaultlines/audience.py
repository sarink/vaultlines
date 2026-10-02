"""Who can see each vault. The hook's session label is built from these answers.

Who can see a vault comes from GitHub (see github.py):
  - no remote                -> only you
  - public repo              -> everyone
  - private repo, you push   -> its collaborators (includes org base permission, teams, owners)
  - anything else            -> unknown, which the label treats as "only you"

Answers are cached in ~/.vaultlines/state/state.json, with the time they were checked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .util import read_json, state_dir, write_json

DAY = 24 * 60 * 60


@dataclass(frozen=True)
class Audience:
    kind: str  # "me" (no remote), "people", "everyone" or "unknown"
    logins: tuple[str, ...] = ()
    reason: str = ""  # why it's unknown
    checked_at: float = 0

    def describe(self) -> str:
        if self.kind == "me":
            return "only you (no remote)"
        if self.kind == "everyone":
            return "everyone (public repo)"
        if self.kind == "unknown":
            return f"unknown: {self.reason}"
        return ", ".join(self.logins) or "nobody"

    def to_json(self) -> dict:
        return {"kind": self.kind, "logins": list(self.logins), "reason": self.reason, "checked_at": self.checked_at}

    @classmethod
    def from_json(cls, data: dict) -> Audience:
        return cls(data["kind"], tuple(data.get("logins", [])), data.get("reason", ""), data.get("checked_at", 0))


def unknown(reason: str) -> Audience:
    return Audience("unknown", reason=reason, checked_at=time.time())


# ---------------------------------------------------------------- state

def state_path():
    return state_dir() / "state.json"


def load_state() -> dict:
    return read_json(state_path(), {})


def save_state(state: dict) -> None:
    write_json(state_path(), state)


# ---------------------------------------------------------------- asking GitHub

def audiences(found: dict, fresh: bool = True) -> tuple[dict[str, Audience], list[str]]:
    """Vault ID -> who can see it, for the vaults in `found` (ID -> vaults.Vault). Also returns
    errors for vaults GitHub couldn't be asked about.

    fresh=True asks GitHub; if it can't be reached, the last cached answer is used.
    fresh=False only reads the cache (for `vl status`).
    """
    from . import github
    from .vaults import remote_id

    state = load_state()
    cache = state.setdefault("audiences", {})
    out: dict[str, Audience] = {}
    errors: list[str] = []
    for vault_id, v in sorted(found.items()):
        if not v.remote:
            out[vault_id] = Audience("me")
            continue
        repo = remote_id(v.remote)
        if repo != vault_id:
            out[vault_id] = unknown(f"its git remote is {v.remote}, not the GitHub repo {vault_id}")
            continue
        cached = Audience.from_json(cache[repo]) if repo in cache else None
        if not fresh:
            out[vault_id] = cached or unknown("not checked yet. Run `vl check`.")
            continue
        try:
            answer = github.audience(repo)
        except github.Unreachable as e:
            errors.append(f"{vault_id}: couldn't ask GitHub ({e})")
            out[vault_id] = cached or unknown(f"couldn't ask GitHub ({e})")
            continue
        cache[repo] = answer.to_json()
        out[vault_id] = answer
    if fresh:
        state["me"] = github.login() or state.get("me", "")
    save_state(state)
    return out, errors


def cached_me() -> str:
    return load_state().get("me", "")
