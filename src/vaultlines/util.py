"""Small helpers shared by every module."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


class VlError(Exception):
    """An error with a message meant for the user."""


def run(
    cmd: list[str], cwd: Path | str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output."""
    try:
        result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    except FileNotFoundError:
        raise VlError(f"Command not found: {cmd[0]}") from None
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise VlError(f"`{' '.join(cmd)}` failed:\n{detail}")
    return result


def home() -> Path:
    return Path(os.path.expanduser("~"))


def expand(path: str | Path) -> Path:
    """Expand ~ and make absolute, following symlinks (as Claude Code does)."""
    return Path(os.path.expanduser(str(path))).resolve()


def vl_home() -> Path:
    """Everything vl owns: ~/.vaultlines, or VL_HOME (tests)."""
    found = os.environ.get("VL_HOME")
    return Path(os.path.abspath(os.path.expanduser(found))) if found else home() / ".vaultlines"


def state_dir() -> Path:
    """Files only vl writes: runtime.json (the hook reads it), sessions, state.json."""
    return vl_home() / "state"


def cache_dir() -> Path:
    """Files vl can always make again."""
    return vl_home() / "cache"


def vaults_dir() -> Path:
    """vaults/OWNER/REPO for each vault. vaults/OWNER exists = OWNER is joined."""
    return vl_home() / "vaults"


def google_dir() -> Path:
    """Read-only Google logins for fetching originals. Sessions can't touch it."""
    return vl_home() / "google"


def clones_path() -> Path:
    """The repos Claude ran in, recorded by the hook: top folder -> OWNER/REPO."""
    return state_dir() / "clones.json"


def fetch_dir(vault_id: str) -> Path:
    """Where `vl source fetch` puts the originals of a vault with a source: cache/fetch/OWNER/REPO."""
    return cache_dir() / "fetch" / vault_id


def refresh_dir(vault_id: str) -> Path:
    """Where `vl source refresh --fetch-only` leaves what changed, for the convert: cache/refresh/OWNER/REPO."""
    return cache_dir() / "refresh" / vault_id


def inside(path: str, folder: str) -> bool:
    """True if `path` is `folder` or something in it. Both must be absolute and normalized."""
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def closest_parent(folders, path: str) -> str | None:
    """The deepest of `folders` that contains `path`, or None."""
    best = None
    for f in folders:
        if inside(path, f) and (best is None or len(f) > len(best)):
            best = f
    return best


def contract(path: str | Path) -> str:
    """Show a path with ~ for the home folder."""
    text = str(path)
    for h in {str(home()), str(home().resolve())}:
        if text == h or text.startswith(h + "/"):
            return "~" + text[len(h):]
    return text


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text()
    return json.loads(text) if text.strip() else default


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".vl-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def say(message: str = "") -> None:
    print(message)


def interactive() -> bool:
    """Can vl ask questions? Only with a person at a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(question: str, choices: list[str] | None = None, check=None) -> str:
    """Ask until the answer isn't empty. With `choices`, the answer is one of them,
    given by its number or its text. With `check`, until check(answer) gives no problem."""
    if choices:
        for i, choice in enumerate(choices, 1):
            print(f"  {i}. {choice}")
    while True:
        try:
            answer = input(f"{question}: ").strip()
        except EOFError:
            raise VlError("Stopped: no answer.") from None
        if choices:
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1]
            if answer in choices:
                return answer
            print(f"Give a number from 1 to {len(choices)}.")
        elif answer:
            problem = check(answer) if check else None
            if not problem:
                return answer
            print(problem)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def notify(message: str) -> None:
    """Show a macOS notification. Does nothing elsewhere, or in tests."""
    if sys.platform == "darwin" and os.environ.get("VL_NO_NOTIFY") != "1":
        script = f"display notification {json.dumps(message)} with title \"vaultlines\""
        subprocess.run(["osascript", "-e", script], capture_output=True)
