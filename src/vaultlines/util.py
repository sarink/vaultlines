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
