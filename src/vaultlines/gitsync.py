"""Each vault is a git repo. Syncing = commit, pull --rebase, push."""

from __future__ import annotations

import getpass
import socket
from pathlib import Path

from .util import VlError, run

GITIGNORE = ["sessions/", ".obsidian/", ".DS_Store"]
GITATTRIBUTES = ["*.md merge=union"]


def git(path: Path, *args: str, check: bool = True):
    return run(["git", "-C", str(path), *args], check=check)


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _ensure_lines(file: Path, lines: list[str], header: str) -> None:
    existing = file.read_text().splitlines() if file.exists() else []
    missing = [line for line in lines if line not in existing]
    if missing:
        with file.open("a") as f:
            if not existing:
                f.write(header + "\n")
            f.write("\n".join(missing) + "\n")


def ensure_identity(path: Path) -> None:
    """Commits need a name. Use the GitHub login, or the computer user."""
    if git(path, "config", "user.email", check=False).stdout.strip():
        return
    login = run(["gh", "api", "user", "--jq", ".login"], check=False).stdout.strip()
    name = login or getpass.getuser()
    git(path, "config", "user.name", name)
    git(path, "config", "user.email", f"{name}@users.noreply.github.com" if login else f"{name}@{socket.gethostname()}")


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not is_repo(path):
        git(path, "init", "-q", "-b", "main")
    _ensure_lines(path / ".gitignore", GITIGNORE, "# Session checkpoints stay on each computer")
    _ensure_lines(path / ".gitattributes", GITATTRIBUTES, "# When two people edit one note, keep both sides")
    ensure_identity(path)
    git(path, "add", "-A")
    if git(path, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(path, "commit", "-q", "-m", "Set up vault")


def remote_url(path: Path) -> str | None:
    result = git(path, "remote", "get-url", "origin", check=False)
    return result.stdout.strip() or None


def set_remote(path: Path, url: str) -> None:
    if remote_url(path):
        git(path, "remote", "set-url", "origin", url)
    else:
        git(path, "remote", "add", "origin", url)


def create_github_repo(path: Path, owner_repo: str) -> str:
    """Create a private GitHub repo from the vault and push it."""
    run(["gh", "repo", "create", owner_repo, "--private", "--source", str(path),
         "--remote", "origin", "--push"])
    return f"https://github.com/{owner_repo}.git"


def clone(url: str, path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise VlError(f"{path} already exists and isn't empty.")
    run(["git", "clone", "-q", url, str(path)])


def branch(path: Path) -> str:
    return git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def pending_changes(path: Path) -> int:
    return len(git(path, "status", "--porcelain", check=False).stdout.splitlines())


def last_commit_age(path: Path) -> str:
    return git(path, "log", "-1", "--format=%cr", check=False).stdout.strip() or "never"


def sync(path: Path) -> str:
    """Commit local changes, get others' changes, send ours. Returns a short status."""
    if not is_repo(path):
        raise VlError(f"{path} is not a git repo. Run `vl apply` to set it up.")
    git(path, "add", "-A")
    if git(path, "diff", "--cached", "--quiet", check=False).returncode != 0:
        who = f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"
        git(path, "commit", "-q", "-m", f"Notes from {who}")
    if not remote_url(path):
        return "committed (no remote)"
    b = branch(path)
    remote_has_branch = bool(git(path, "ls-remote", "--heads", "origin", b).stdout.strip())
    if remote_has_branch and git(path, "pull", "-q", "--rebase", "origin", b, check=False).returncode != 0:
        git(path, "rebase", "--abort", check=False)
        raise VlError("couldn't get new notes (pull failed)")
    if git(path, "push", "-q", "-u", "origin", b, check=False).returncode != 0:
        raise VlError("couldn't send notes (push failed)")
    return "synced"


def pull_only(path: Path) -> None:
    git(path, "pull", "-q", "--ff-only")
