"""Which vaults a Claude session uses: where it saves notes (`writes`) and what else it may
read (`reads`).

1. Which repo is this? The git repo that holds the folder where Claude started, and its
   GitHub remotes (origin first), read from .git/config. No git command runs.
2. Did you join the remote's owner? Then [repos."OWNER/REPO"] in config.toml, else the
   owner's vault whose notes_from lists the repo, else your personal vault for that
   owner. Reads: the owner's other vaults.
3. If not: the closest [folders."path"] entry in config.toml.
4. If nothing matched: your own personal vault, and nothing else.

Standard library only, and pure apart from reading .git files: the hook imports it.
"""

from __future__ import annotations

import os
import re

GITHUB_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
SECTION_RE = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\](.*)$')


# ---------------------------------------------------------------- reading git's files

def _value(text: str) -> str:
    """A git config value: quotes removed, comments after it dropped."""
    out, quoted, i = [], False, 0
    text = text.strip()
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            out.append({"n": "\n", "t": "\t"}.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        elif c in "#;" and not quoted:
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).strip()


def remotes(config_text: str) -> list[tuple[str, str]]:
    """(name, url) for each remote in a .git/config, origin first, then in file order."""
    found: dict[str, str] = {}
    section = None
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = SECTION_RE.match(line)
        if m:
            section = (m.group(1).lower(), m.group(2))
            line = m.group(3).strip()
            if not line:
                continue
        if section and section[0] == "remote" and section[1] is not None:
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "url" and section[1] not in found:
                found[section[1]] = _value(value)
    names = sorted(found, key=lambda n: (n != "origin",))
    return [(n, found[n]) for n in names]


def find_repo(start: str) -> tuple[str, str] | None:
    """(the repo's top folder, the folder that holds its config) for the git repo that
    contains `start`. Follows `.git` files (worktrees, submodules)."""
    d = os.path.realpath(start) if start else ""
    while d:
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            return d, _common(dot)
        if os.path.isfile(dot):
            try:
                with open(dot) as f:
                    text = f.read().strip()
            except OSError:
                text = ""
            if text.startswith("gitdir:"):
                gitdir = text[len("gitdir:"):].strip()
                gitdir = os.path.realpath(os.path.join(d, gitdir))
                return d, _common(gitdir)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def _common(gitdir: str) -> str:
    """A worktree's gitdir points to the main repo's through `commondir`."""
    try:
        with open(os.path.join(gitdir, "commondir")) as f:
            return os.path.realpath(os.path.join(gitdir, f.read().strip()))
    except OSError:
        return gitdir


def repo_id(url: str) -> str | None:
    """OWNER/REPO in lowercase for a GitHub remote. In tests, file:// remotes count too."""
    m = GITHUB_RE.match(url.strip())
    if m:
        return f"{m.group(1)}/{m.group(2)}".lower()
    if os.environ.get("VAULTLINES_TEST_REMOTES") == "1" and url.startswith("file://"):
        parts = url[len("file://"):].rstrip("/").removesuffix(".git").split("/")
        if len(parts) >= 2 and parts[-2] and parts[-1]:
            return f"{parts[-2]}/{parts[-1]}".lower()
    return None


def repo_of(start: str) -> tuple[str, list[str]] | None:
    """(top folder, [OWNER/REPO of each GitHub remote, origin first]) for the repo holding `start`."""
    found = find_repo(start)
    if not found:
        return None
    top, gitdir = found
    try:
        with open(os.path.join(gitdir, "config")) as f:
            text = f.read()
    except OSError:
        text = ""
    ids = []
    for _, url in remotes(text):
        rid = repo_id(url)
        if rid and rid not in ids:
            ids.append(rid)
    return top, ids


# ---------------------------------------------------------------- the rules

def _merge(*lists, without=None) -> list[str]:
    out = []
    for items in lists:
        for v in items or []:
            if v and v != without and v not in out:
                out.append(v)
    return out


def _for_repo(repo: str, root: str, owner: dict, runtime: dict) -> dict:
    repos = runtime.get("repos") or {}
    exact = repos.get(repo) or {}
    wide = repos.get(repo.split("/")[0] + "/*") or {}
    conflict = (owner.get("conflicts") or {}).get(repo)
    notes_from = owner.get("notes_from") or {}
    if exact.get("writes") or wide.get("writes"):
        writes, how = exact.get("writes") or wide.get("writes"), "repos"
    elif repo in notes_from:
        writes, how = notes_from[repo], "notes_from"
    elif conflict:
        writes, how = owner.get("personal"), "conflict"
    else:
        writes, how = owner.get("personal"), "personal"
    reads = _merge([v for v in owner.get("vaults") or [] if v != writes], wide.get("reads"), exact.get("reads"),
                   without=writes)
    out = {"writes": writes, "reads": reads, "how": how, "repo": repo, "root": root, "folder": None}
    if conflict:
        out["conflict"] = list(conflict)
    return out


def resolve(project_dir: str, runtime: dict) -> dict:
    """The session's rules: {"writes": vault, "reads": [vaults], "how": why, "repo": OWNER/REPO
    or None, "root": the repo's top folder or None, "folder": the [folders] entry or None}.
    Vaults are short names from runtime.json."""
    from .util import closest_parent

    found = repo_of(project_dir) if project_dir else None
    root, ids = found if found else (None, [])
    owners = runtime.get("owners") or {}
    for rid in ids:
        owner = owners.get(rid.split("/")[0])
        if owner is not None:
            return _for_repo(rid, root, owner, runtime)
    default = runtime.get("default") or {}
    folders = runtime.get("folders") or {}
    key = closest_parent(folders, os.path.realpath(project_dir)) if project_dir else None
    if key:
        entry = folders[key]
        writes = entry.get("writes") or default.get("writes")
        return {"writes": writes, "reads": _merge(entry.get("reads"), without=writes), "how": "folder",
                "repo": ids[0] if ids else None, "root": root, "folder": key}
    return {"writes": default.get("writes"), "reads": _merge(default.get("reads")), "how": "default",
            "repo": ids[0] if ids else None, "root": root, "folder": None}
