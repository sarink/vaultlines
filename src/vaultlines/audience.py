"""Who can see each vault. The hook's session label is built from these answers.

Who can see a vault comes from GitHub:
  - no remote                -> only you
  - public repo              -> everyone
  - private repo, you push   -> its collaborators (includes org base permission, teams, owners)
  - anything else            -> unknown, which the label treats as "only you"

Answers are cached in ~/.config/vaultlines/state.json, with the time they were checked.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from . import config, gitsync
from .config import Config
from .util import VlError, read_json, run, write_json

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
    return config.config_path().with_name("state.json")


def load_state() -> dict:
    return read_json(state_path(), {})


def save_state(state: dict) -> None:
    write_json(state_path(), state)


# ---------------------------------------------------------------- asking GitHub

class Unreachable(Exception):
    """GitHub couldn't be asked (network, sign-in). Not an answer about access."""


def _fake() -> dict | None:
    """Tests set VAULTLINES_FAKE_AUDIENCE to {"me": login, remote: [logins] | "everyone" | "unknown"}."""
    text = os.environ.get("VAULTLINES_FAKE_AUDIENCE")
    return json.loads(text) if text else None


def me() -> str:
    """Your GitHub login, or "" if unknown (which makes the check stricter, never looser)."""
    fake = _fake()
    if fake is not None:
        return fake.get("me", "")
    try:
        return run(["gh", "api", "user", "--jq", ".login"], check=False).stdout.strip()
    except VlError:
        return ""


def _gh(path: str, *extra: str) -> str:
    try:
        result = run(["gh", "api", *extra, path], check=False)
    except VlError as e:
        raise Unreachable(str(e)) from None
    if result.returncode == 0:
        return result.stdout
    detail = (result.stderr or result.stdout).strip()
    message = detail.splitlines()[0] if detail else f"gh api {path} failed"
    # 403 and 404 are answers: you can't list this repo. Anything else means GitHub wasn't reached.
    if re.search(r"HTTP 40[34]", detail) and "rate limit" not in detail.lower():
        raise VlError(message.removeprefix("gh: "))
    raise Unreachable(message)


def fetch(remote: str) -> Audience:
    """Ask GitHub who can see a repo. Raises Unreachable if it can't be asked."""
    now = time.time()
    fake = _fake()
    if fake is not None:
        value = fake.get(remote, "unknown")
        if value in ("everyone", "unknown"):
            return Audience(value, reason="not listed in VAULTLINES_FAKE_AUDIENCE", checked_at=now)
        return Audience("people", tuple(sorted(value)), checked_at=now)

    owner, repo = gitsync.parse_github(remote)
    try:
        info = json.loads(_gh(f"repos/{owner}/{repo}"))
    except VlError as e:
        return unknown(f"GitHub says: {e}")
    visibility = info.get("visibility") or ("private" if info.get("private") else "public")
    if visibility == "public":
        return Audience("everyone", checked_at=now)
    if visibility != "private":
        return unknown(f"it's an {visibility} repo, and GitHub can't list everyone who can see those")
    if not (info.get("permissions") or {}).get("push"):
        return unknown("you have read-only access, so GitHub won't list who can see it")
    try:
        out = _gh(f"repos/{owner}/{repo}/collaborators?affiliation=all&per_page=100",
                  "--paginate", "--jq", ".[].login")
    except VlError as e:
        return unknown(f"GitHub says: {e}")
    return Audience("people", tuple(sorted(set(out.split()), key=str.lower)), checked_at=now)


def audiences(cfg: Config, fresh: bool = True) -> tuple[dict[str, Audience], list[str]]:
    """Vault name -> who can see it. Also returns errors for vaults GitHub couldn't be asked about.

    fresh=True asks GitHub; if it can't be reached, the last cached answer is used.
    fresh=False only reads the cache (for `vl status`).
    """
    state = load_state()
    cache = state.setdefault("audiences", {})
    out: dict[str, Audience] = {}
    errors: list[str] = []
    for v in sorted(cfg.vaults.values(), key=lambda v: v.name):
        origin = gitsync.remote_url(v.path) if gitsync.is_repo(v.path) else None
        if origin and (not v.remote or gitsync.repo_key(origin) != gitsync.repo_key(v.remote)):
            out[v.name] = unknown(f"its git remote is {origin}, but the config says {v.remote or 'no remote'}")
            continue
        if not v.remote:
            out[v.name] = Audience("me")
            continue
        key = gitsync.repo_key(v.remote)
        cached = Audience.from_json(cache[key]) if key in cache else None
        if not fresh:
            out[v.name] = cached or unknown("not checked yet. Run `vl check`.")
            continue
        try:
            answer = fetch(v.remote)
        except Unreachable as e:
            errors.append(f"{v.name}: couldn't ask GitHub ({e})")
            out[v.name] = cached or unknown(f"couldn't ask GitHub ({e})")
            continue
        cache[key] = answer.to_json()
        out[v.name] = answer
    if fresh:
        state["me"] = me() or state.get("me", "")
    save_state(state)
    return out, errors


def cached_me() -> str:
    return load_state().get("me", "")
