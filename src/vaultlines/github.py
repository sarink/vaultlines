"""Everything vl asks GitHub, through `gh`.

GitHub decides who gets which vault: discovery lists the `vault-` repos with a
vault.toml that you can access, and who can see a vault is who GitHub says can see
its repo.

Tests set VAULTLINES_FAKE_GITHUB to a JSON file in place of GitHub, and
VAULTLINES_FAKE_LOGIN to who they are:

    {"root": "/tmp/remotes",                        # bare repos: root/OWNER/REPO.git
     "orgs": {"mixim-ai": ["alice", "bob"]},
     "repos": {"mixim-ai/vault-public": {"push": ["alice"], "read": ["bob"]},
               "carol/vault-open": "public"}}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .audience import Audience, unknown
from .util import VlError, run

VAULT_PREFIX = "vault-"


class Unreachable(Exception):
    """GitHub couldn't be asked (network, login). Not an answer about access."""


# ---------------------------------------------------------------- the fake

def _fake_path() -> Path | None:
    found = os.environ.get("VAULTLINES_FAKE_GITHUB")
    return Path(found) if found else None


def _fake() -> dict | None:
    path = _fake_path()
    return json.loads(path.read_text()) if path else None


def _fake_save(data: dict) -> None:
    _fake_path().write_text(json.dumps(data, indent=1))


def _fake_access(data: dict, repo_id: str, who: str) -> str | None:
    """"push", "read" or None."""
    entry = data["repos"].get(repo_id)
    if entry == "public":
        return "read"
    if isinstance(entry, dict):
        if who in entry.get("push", []):
            return "push"
        if who in entry.get("read", []):
            return "read"
    return None


def _fake_bare(data: dict, repo_id: str) -> Path:
    return Path(data["root"]) / f"{repo_id}.git"


# ---------------------------------------------------------------- gh

def _gh(path: str, *extra: str) -> str:
    """`gh api PATH`. A 403 or 404 is an answer (VlError); anything else is Unreachable."""
    try:
        result = run(["gh", "api", *extra, path], check=False)
    except VlError as e:
        raise Unreachable(str(e)) from None
    if result.returncode == 0:
        return result.stdout
    detail = (result.stderr or result.stdout).strip()
    message = detail.splitlines()[0] if detail else f"gh api {path} failed"
    if re.search(r"HTTP 40[34]", detail) and "rate limit" not in detail.lower():
        raise VlError(message.removeprefix("gh: "))
    raise Unreachable(message)


def logged_in() -> bool:
    if _fake_path():
        return True
    return run(["gh", "auth", "status"], check=False).returncode == 0


def scopes() -> set[str]:
    """What your gh login may do, like {"repo", "workflow"}."""
    if _fake_path():
        return {"repo", "workflow"}
    out = run(["gh", "auth", "status", "--hostname", "github.com"], check=False)
    m = re.search(r"Token scopes:\s*(.*)", out.stdout + out.stderr)
    return {s.strip(" '\"") for s in m.group(1).split(",")} if m else set()


def login() -> str:
    """Your GitHub login in lowercase, or "" if unknown."""
    if _fake_path():
        return os.environ.get("VAULTLINES_FAKE_LOGIN", "").lower()
    try:
        return run(["gh", "api", "user", "--jq", ".login"], check=False).stdout.strip().lower()
    except VlError:
        return ""


def owner_kind(owner: str) -> str | None:
    """"User", "Organization", or None if there's no such account."""
    data = _fake()
    if data is not None:
        if owner in data["orgs"]:
            return "Organization"
        users = {os.environ.get("VAULTLINES_FAKE_LOGIN", "")} | {m for ms in data["orgs"].values() for m in ms}
        users |= {r.split("/")[0] for r in data["repos"] if r.split("/")[0] not in data["orgs"]}
        return "User" if owner in users else None
    try:
        return _gh(f"users/{owner}", "--jq", ".type").strip() or None
    except VlError:
        return None


def clone_url(repo_id: str) -> str:
    data = _fake()
    if data is not None:
        return f"file://{_fake_bare(data, repo_id)}"
    return f"https://github.com/{repo_id}.git"


def _has_vault_toml(repo_id: str) -> bool:
    data = _fake()
    if data is not None:
        bare = _fake_bare(data, repo_id)
        return run(["git", "--git-dir", str(bare), "cat-file", "-e", "HEAD:vault.toml"], check=False).returncode == 0
    try:
        return _gh(f"repos/{repo_id}/contents/vault.toml", "--jq", ".type").strip() == "file"
    except VlError:
        return False


def vault_repos(owner: str) -> dict[str, str]:
    """The owner's vaults you can access: OWNER/REPO -> clone URL. A vault's repo name starts
    with `vault-` and it has vault.toml at its root. Raises Unreachable if GitHub can't be asked."""
    me = login()
    data = _fake()
    if data is not None:
        names = [r for r in data["repos"] if r.split("/")[0] == owner and _fake_access(data, r, me)]
    else:
        if owner == me:
            path = "user/repos?affiliation=owner&per_page=100"
        elif owner_kind(owner) == "Organization":
            path = f"orgs/{owner}/repos?type=all&per_page=100"
        else:
            path = "user/repos?affiliation=collaborator&per_page=100"
        try:
            out = _gh(path, "--paginate", "--jq", ".[] | .full_name")
        except VlError as e:
            raise Unreachable(str(e)) from None
        names = [n.strip().lower() for n in out.splitlines() if n.strip()]
        names = [n for n in names if n.split("/")[0] == owner]
    found = {}
    for repo_id in sorted(set(names)):
        if repo_id.split("/", 1)[1].startswith(VAULT_PREFIX) and _has_vault_toml(repo_id):
            found[repo_id] = clone_url(repo_id)
    return found


def exists(repo_id: str) -> bool:
    data = _fake()
    if data is not None:
        return repo_id in data["repos"]
    try:
        _gh(f"repos/{repo_id}", "--jq", ".full_name")
        return True
    except VlError:
        return False


def canonical(repo_id: str) -> str | None:
    """The repo's name now (GitHub follows renames), or None if you can't see it."""
    data = _fake()
    if data is not None:
        renames = data.get("renames", {})
        repo_id = renames.get(repo_id, repo_id)
        return repo_id if repo_id in data["repos"] else None
    try:
        return _gh(f"repos/{repo_id}", "--jq", ".full_name").strip().lower() or None
    except VlError:
        return None


def create_private_repo(repo_id: str, path: Path) -> str:
    """Create a private repo from a local vault, set it as origin and push. Returns its URL."""
    if exists(repo_id):
        raise VlError(f"{repo_id} already exists on GitHub.")
    data = _fake()
    if data is not None:
        bare = _fake_bare(data, repo_id)
        run(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
        data["repos"][repo_id] = {"push": [login()]}
        _fake_save(data)
        url = clone_url(repo_id)
        run(["git", "-C", str(path), "remote", "add", "origin", url])
        run(["git", "-C", str(path), "push", "-q", "-u", "origin", "HEAD"])
        return url
    run(["gh", "repo", "create", repo_id, "--private", "--source", str(path), "--remote", "origin", "--push"])
    return clone_url(repo_id)


def audience(repo_id: str) -> Audience:
    """Who can see a repo. Raises Unreachable if GitHub can't be asked."""
    now = time.time()
    data = _fake()
    if data is not None:
        entry = data["repos"].get(repo_id)
        if entry == "public":
            return Audience("everyone", checked_at=now)
        access = _fake_access(data, repo_id, login())
        if access is None:
            return unknown("GitHub says: Not Found (HTTP 404)")
        if access != "push":
            return unknown("you have read-only access, so GitHub won't list who can see it")
        logins = sorted(set(entry.get("push", []) + entry.get("read", [])), key=str.lower)
        return Audience("people", tuple(logins), checked_at=now)
    try:
        info = json.loads(_gh(f"repos/{repo_id}"))
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
        out = _gh(f"repos/{repo_id}/collaborators?affiliation=all&per_page=100", "--paginate", "--jq", ".[].login")
    except VlError as e:
        return unknown(f"GitHub says: {e}")
    return Audience("people", tuple(sorted(set(out.split()), key=str.lower)), checked_at=now)


# ---------------------------------------------------------------- Actions (for gdrive)

def set_secret(repo_id: str, name: str, value: str) -> None:
    """Set an Actions secret. The value goes through stdin, never the command line."""
    data = _fake()
    if data is not None:
        data.setdefault("secrets", {}).setdefault(repo_id, {})[name] = value
        _fake_save(data)
        return
    result = subprocess.run(["gh", "secret", "set", name, "--repo", repo_id], input=value, text=True,
                            capture_output=True)
    if result.returncode != 0:
        raise VlError(f"Couldn't set the secret {name} in {repo_id}:\n{(result.stderr or result.stdout).strip()}")


def run_workflow(repo_id: str, workflow: str, inputs: dict[str, str] | None = None) -> None:
    data = _fake()
    if data is not None:
        data.setdefault("workflow_runs", []).append({"repo": repo_id, "workflow": workflow, "inputs": inputs or {}})
        _fake_save(data)
        return
    fields = [a for k, v in (inputs or {}).items() for a in ("-f", f"{k}={v}")]
    run(["gh", "workflow", "run", workflow, "--repo", repo_id, *fields])
