"""The background sync job (macOS launchd)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .util import home, run, state_dir, vl_home

LABEL = "com.vaultlines.sync"


def plist_path() -> Path:
    return home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return state_dir() / "sync.log"


def supported() -> bool:
    return sys.platform == "darwin" and os.environ.get("VAULTLINES_NO_LAUNCHD") != "1"


def _vl_path() -> str:
    """The vl on PATH. One in a project's .venv (first on PATH under `uv run`) comes last:
    the job should keep working when that project changes."""
    found = [os.path.join(d, "vl") for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    found = [f for f in found if os.path.isfile(f) and os.access(f, os.X_OK)]
    found.sort(key=lambda f: ".venv" in Path(f).parts)
    return found[0] if found else str(Path(sys.argv[0]).resolve())


def _path_env() -> str:
    dirs = []
    for tool in ("git", "gh", "uv", "uvx", "claude"):
        found = shutil.which(tool)
        if found and os.path.dirname(found) not in dirs:
            dirs.append(os.path.dirname(found))
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def _plist(interval: int) -> str:
    found = os.environ.get("VAULTLINES_HOME")
    custom_home = f"<key>VAULTLINES_HOME</key><string>{vl_home()}</string>" if found else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{_vl_path()}</string><string>sync</string><string>--background</string></array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{_path_env()}</string>{custom_home}</dict>
  <key>StandardOutPath</key><string>{log_path()}</string>
  <key>StandardErrorPath</key><string>{log_path()}</string>
</dict>
</plist>
"""


def install(interval: int) -> None:
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(_plist(interval))
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
    run(["launchctl", "bootstrap", domain, str(plist)])


def keep_current(interval: int) -> bool:
    """`vl apply`: write the job again if it changed (a new sync_interval, or vl moved).
    Without a job (never set up, or turned off), does nothing. True if it wrote it."""
    if not supported() or not plist_path().exists() or plist_path().read_text() == _plist(interval):
        return False
    install(interval)
    return True


def uninstall() -> None:
    if not supported():  # also keeps tests from stopping the real job: launchd is per user, not per HOME
        return
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
    plist_path().unlink(missing_ok=True)


def loaded() -> bool:
    return run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False).returncode == 0
