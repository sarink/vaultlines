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
    return shutil.which("vl") or str(Path(sys.argv[0]).resolve())


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


def install(interval: int) -> None:
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    found = os.environ.get("VAULTLINES_HOME")
    custom_home = f"<key>VAULTLINES_HOME</key><string>{vl_home()}</string>" if found else ""
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
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
""")
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
    run(["launchctl", "bootstrap", domain, str(plist)])


def uninstall() -> None:
    if not supported():  # also keeps tests from stopping the real job: launchd is per user, not per HOME
        return
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
    plist_path().unlink(missing_ok=True)


def loaded() -> bool:
    return run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False).returncode == 0
