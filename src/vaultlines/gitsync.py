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
    from .github import login as github_login

    login = github_login()
    name = login or getpass.getuser()
    git(path, "config", "user.name", name)
    git(path, "config", "user.email", f"{name}@users.noreply.github.com" if login else f"{name}@{socket.gethostname()}")


def ensure_local_rules(path: Path) -> None:
    """vl's git rules for this clone, whatever the vault's own files say (a vault made by
    hand may have none): session checkpoints stay on this computer, and when two people
    edit one note, both sides are kept."""
    info = path / ".git" / "info"
    if (path / ".git").is_dir():
        info.mkdir(parents=True, exist_ok=True)
        _ensure_lines(info / "exclude", GITIGNORE, "# Added by vaultlines: these stay on this computer")
        _ensure_lines(info / "attributes", GITATTRIBUTES, "# Added by vaultlines: keep both sides of a note")


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not is_repo(path):
        git(path, "init", "-q", "-b", "main")
    _ensure_lines(path / ".gitignore", GITIGNORE, "# Session checkpoints stay on each computer")
    _ensure_lines(path / ".gitattributes", GITATTRIBUTES, "# When two people edit one note, keep both sides")
    ensure_local_rules(path)
    ensure_identity(path)
    git(path, "add", "-A")
    if git(path, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(path, "commit", "-q", "-m", "Set up vault")


def in_work_tree(path: Path) -> bool:
    result = run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], check=False)
    return result.stdout.strip() == "true"


def remote_url(path: Path) -> str | None:
    result = git(path, "remote", "get-url", "origin", check=False)
    return result.stdout.strip() or None


def set_remote(path: Path, url: str) -> None:
    if remote_url(path):
        git(path, "remote", "set-url", "origin", url)
    else:
        git(path, "remote", "add", "origin", url)


def has_commits(path: Path) -> bool:
    return git(path, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


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


def commit(path: Path, message: str) -> bool:
    """Commit every change in the vault. False if there was nothing to commit."""
    git(path, "add", "-A")
    if git(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(path, "commit", "-q", "-m", message)
    return True


def sync(path: Path) -> str:
    """Commit local changes, get others' changes, send ours. Returns a short status."""
    if not is_repo(path):
        raise VlError(f"{path} is not a git repo. Run `vl apply` to set it up.")
    ensure_local_rules(path)
    committed = commit(path, f"Notes from {getpass.getuser()}@{socket.gethostname().split('.')[0]}")
    if not remote_url(path):
        return "committed (no remote)" if committed else "synced"
    b = branch(path)
    remote_has_branch = bool(git(path, "ls-remote", "--heads", "origin", b).stdout.strip())
    if remote_has_branch and git(path, "pull", "-q", "--rebase", "origin", b, check=False).returncode != 0:
        git(path, "rebase", "--abort", check=False)
        raise VlError("couldn't get new notes (pull failed)")
    if remote_has_branch and git(path, "rev-list", "--count", f"origin/{b}..{b}").stdout.strip() == "0":
        return "synced"  # nothing to send (so read-only vaults sync fine)
    if git(path, "push", "-q", "-u", "origin", b, check=False).returncode != 0:
        raise VlError("couldn't send notes (push failed)")
    return "synced"


def pull_only(path: Path) -> None:
    git(path, "pull", "-q", "--ff-only")


def pull_keeping_changes(path: Path) -> str:
    """For vaults with a source: take the remote as it is. Local changes, which
    shouldn't happen, are saved on a branch `local-changes-DATE` first."""
    import time

    if not is_repo(path):
        raise VlError(f"{path} is not a git repo.")
    if not remote_url(path):
        return "no remote"
    b = branch(path)
    if git(path, "fetch", "-q", "origin", check=False).returncode != 0:
        raise VlError("couldn't get new notes (fetch failed)")
    if not git(path, "rev-parse", "--verify", "-q", f"origin/{b}", check=False).stdout.strip():
        return "nothing on GitHub yet"
    dirty = pending_changes(path) > 0
    ahead = git(path, "rev-list", "--count", f"origin/{b}..HEAD", check=False).stdout.strip() not in ("", "0")
    saved = ""
    if dirty or ahead:
        saved = f"local-changes-{time.strftime('%Y-%m-%d-%H%M%S')}"
        git(path, "checkout", "-q", "-b", saved)
        git(path, "add", "-A", "--", ".", ":!sessions", ":!.obsidian")  # these stay on this computer
        if git(path, "diff", "--cached", "--quiet", check=False).returncode != 0:
            git(path, "commit", "-q", "-m", "Local changes, saved by vl before taking the vault from GitHub")
        git(path, "checkout", "-q", b)
    git(path, "reset", "-q", "--hard", f"origin/{b}")
    git(path, "clean", "-q", "-fd", "-e", "sessions/", "-e", ".obsidian/")
    if saved:
        return (f"synced. This vault is refreshed on GitHub, so local changes were moved to the branch "
                f"{saved}")
    return "synced"
