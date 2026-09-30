"""The session label: who may see what a Claude session has read so far.

A label is None ("everyone": the session has read nothing restricted yet), or a
frozenset of lowercase GitHub logins who can see every vault the session read.
You are always included, so an empty set means "only you".

Audiences come from runtime.json as dicts: {"kind": "me" | "people" | "everyone" |
"unknown", "logins": [...], "reason": "..."}.

Kept free of heavy imports: the hook loads this on every tool call.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

Label = frozenset | None
EVERYONE: Label = None
ONLY_YOU: Label = frozenset()
SESSION_MAX_AGE = 30 * 24 * 60 * 60


def narrow(label: Label, audience: dict) -> Label:
    """The label after reading a vault with this audience."""
    kind = audience.get("kind")
    if kind == "everyone":
        return label
    if kind == "people":
        logins = frozenset(x.lower() for x in audience.get("logins", []))
        return logins if label is None else label & logins
    return ONLY_YOU  # "me", or "unknown": only you, to be safe


def new_people(label: Label, audience: dict, me: str) -> list[str]:
    """Who would newly see what the session read, if it wrote into a vault with this audience.

    Empty means the write shows nothing to anyone new.
    """
    if label is None:
        return []
    kind = audience.get("kind")
    if kind == "me":
        return []
    if kind == "everyone":
        return ["anyone (it's a public repo)"]
    if kind == "unknown":
        return [f"people vl can't list ({audience.get('reason') or 'unknown audience'})"]
    me = me.lower()
    return [x for x in audience.get("logins", []) if x.lower() not in label and x.lower() != me]


def describe(label: Label, me: str = "") -> str:
    if label is None:
        return "everyone"
    others = sorted(x for x in label if x != me.lower())
    return ", ".join(others) + " and you" if others else "only you"


def names(people: list[str]) -> str:
    return people[0] if len(people) == 1 else ", ".join(people[:-1]) + " and " + people[-1]


def to_json(label: Label):
    return None if label is None else sorted(label)


def from_json(value) -> Label:
    return None if value is None else frozenset(value)


# ---------------------------------------------------------------- session state

def sessions_dir(state_dir: Path) -> Path:
    return state_dir / "sessions"


def _file_name(session_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
        return session_id
    return "h-" + hashlib.sha256(session_id.encode()).hexdigest()[:32]


def session_path(state_dir: Path, session_id: str) -> Path:
    return sessions_dir(state_dir) / f"{_file_name(session_id)}.json"


@contextmanager
def session(state_dir: Path, session_id: str):
    """Load a session's state (or None), locked so parallel tool calls don't lose updates.

    The body gets a one-item list holding the state; replace or change item 0 to save it.
    """
    path = session_path(state_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            before = json.loads(path.read_text()) if path.exists() else None
        except (OSError, ValueError):
            before = None
        box = [None if before is None else dict(before)]
        yield box
        if box[0] is not None and box[0] != before:
            box[0]["updated"] = time.time()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(box[0], indent=1) + "\n")
            tmp.replace(path)


def new_session(folder: str | None, label: Label, source: str) -> dict:
    now = time.time()
    return {"folder": folder, "label": to_json(label), "read": [], "source": source, "started": now, "updated": now}


def all_sessions(state_dir: Path) -> list[tuple[str, dict]]:
    out = []
    for p in sessions_dir(state_dir).glob("*.json"):
        try:
            out.append((p.stem, json.loads(p.read_text())))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda s: s[1].get("updated", 0), reverse=True)


def clean_sessions(state_dir: Path, max_age: float = SESSION_MAX_AGE) -> int:
    """Delete session files not used for `max_age` seconds. Returns how many."""
    removed = 0
    cutoff = time.time() - max_age
    for p in sessions_dir(state_dir).glob("*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += p.suffix == ".json"
        except OSError:
            continue
    return removed

