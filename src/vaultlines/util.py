"""Small helpers shared by every module."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
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


def state_dir() -> Path:
    """Where vl keeps files the hook reads: ~/.local/state/vaultlines."""
    base = os.environ.get("XDG_STATE_HOME") or str(home() / ".local" / "state")
    return Path(base) / "vaultlines"


def cache_dir() -> Path:
    """Where vl keeps files it can always make again: ~/.cache/vaultlines."""
    base = os.environ.get("XDG_CACHE_HOME") or str(home() / ".cache")
    return Path(base) / "vaultlines"


def fetch_dir(vault: str) -> Path:
    """Where `vl fetch` puts a source vault's originals."""
    return cache_dir() / "fetch" / vault


def machine_id() -> str:
    """A random ID for this computer, made once."""
    path = state_dir() / "machine-id"
    try:
        found = path.read_text().strip()
    except FileNotFoundError:
        found = ""
    if not found:
        found = uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(found + "\n")
    return found


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


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def notify(message: str) -> None:
    """Show a macOS notification. Does nothing elsewhere, or in tests."""
    if sys.platform == "darwin" and os.environ.get("VAULTLINES_NO_NOTIFY") != "1":
        script = f"display notification {json.dumps(message)} with title \"vaultlines\""
        subprocess.run(["osascript", "-e", script], capture_output=True)
