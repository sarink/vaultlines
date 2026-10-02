"""Vaults: git repos of notes, each described by its own vault.toml.

A vault is always written OWNER/REPO, in lowercase: `mixim-ai/vault-public`,
`kabir/vault-kabir-personal`. Its clone is vaults/OWNER/REPO in
~/.vaultlines. On GitHub, a vault is a repo whose name starts with `vault-` and that
has vault.toml at its root:

    about      = "Notes everyone at Mixim can see."
    notes_from = ["mixim-ai/marketing", "mixim-ai/studio"]   # repos whose notes go here

    [source]                                                  # filled from elsewhere, read-only
    kind = "gdrive"

Basic Memory and Claude know each vault by a short name: the owner, then the repo
name without `vault-` (see `short_name()`).
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .rules import remotes
from .rules import repo_id as remote_id  # noqa: F401 - OWNER/REPO of a git remote
from .util import vaults_dir

PREFIX = "vault-"
VAULT_FILE = "vault.toml"
OWNER_RE = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
REPO_RE = r"(?!\.\.?$)[a-z0-9._-]+"
ID_RE = re.compile(rf"^{OWNER_RE}/{REPO_RE}$")
REPO_ID_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
KEYS = {"about", "notes_from", "source"}


def valid_id(vault_id: str) -> bool:
    return isinstance(vault_id, str) and bool(ID_RE.match(vault_id))


def personal_id(owner: str, me: str) -> str:
    return f"{owner}/{PREFIX}{me}-personal"


def short_name(vault_id: str) -> str:
    """mixim-ai/vault-public -> mixim-ai-public. kabir/vault-kabir-personal -> kabir-personal."""
    owner, repo = vault_id.split("/", 1)
    rest = repo.removeprefix(PREFIX)
    if rest == owner or rest.startswith(owner + "-"):
        return rest
    return f"{owner}-{rest}"


def short_names(ids) -> dict[str, str]:
    """A short name for each vault, never the same twice. When two would be the same,
    the later one (by ID) keeps the whole repo name."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for vault_id in sorted(ids):
        name = short_name(vault_id)
        if name in taken:
            name = vault_id.replace("/", "-")
            n = 2
            while name in taken:
                name = f"{vault_id.replace('/', '-')}-{n}"
                n += 1
        taken.add(name)
        out[vault_id] = name
    return out


# ---------------------------------------------------------------- vault.toml

@dataclass
class Info:
    """What a vault says about itself."""
    about: str = ""
    notes_from: list[str] = field(default_factory=list)  # OWNER/REPO, lowercase
    source: dict | None = None  # the [source] table: the vault is filled from elsewhere


def parse_vault_toml(text: str) -> tuple[Info, list[str]]:
    """Read vault.toml. Problems are returned, never raised: a vault someone else wrote
    must not stop vl from working."""
    info = Info()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return info, [f"{VAULT_FILE}: {e}"]
    problems = [f"{key}: unknown key. Allowed: {', '.join(sorted(KEYS))}" for key in data if key not in KEYS]
    about = data.get("about", "")
    if isinstance(about, str):
        info.about = about.strip()
    else:
        problems.append("about: should be text")
    notes_from = data.get("notes_from", [])
    if not isinstance(notes_from, list):
        problems.append('notes_from: should be a list of repos, like ["OWNER/REPO"]')
        notes_from = []
    for repo in notes_from:
        if isinstance(repo, str) and REPO_ID_RE.match(repo.strip()):
            if repo.strip().lower() not in info.notes_from:
                info.notes_from.append(repo.strip().lower())
        else:
            problems.append(f"notes_from: {repo!r} isn't OWNER/REPO")
    if "source" in data:
        source = data["source"]
        if not isinstance(source, dict):
            problems.append("source: should be a table, like [source] with kind = \"gdrive\"")
        else:
            info.source = dict(source)
            if not isinstance(source.get("kind"), str):
                problems.append('source.kind: missing, like kind = "gdrive"')
    return info, problems


def _toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def render_vault_toml(about: str, notes_from=(), source: dict | None = None, source_comments: dict | None = None) -> str:
    lines = [f"about      = {_toml_string(about)}"]
    repos = ", ".join(_toml_string(r) for r in notes_from)
    lines.append(f"notes_from = [{repos}]" + ("" if notes_from else '   # repos whose notes go here, like "OWNER/REPO"'))
    if source:
        lines += ["", "[source]"]
        width = max(len(k) for k in source)
        for key, value in source.items():
            note = (source_comments or {}).get(key)
            lines.append(f"{key.ljust(width)} = {_toml_string(value) if isinstance(value, str) else json.dumps(value)}"
                         + (f"   # {note}" if note else ""))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- on disk

@dataclass
class Vault:
    id: str  # OWNER/REPO
    path: Path
    remote: str | None = None  # the git origin; None: this computer only
    info: Info = field(default_factory=Info)
    problems: list[str] = field(default_factory=list)

    @property
    def owner(self) -> str:
        return self.id.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.id.split("/", 1)[1]

    @property
    def about(self) -> str:
        return self.info.about

    @property
    def notes_from(self) -> list[str]:
        return self.info.notes_from

    @property
    def source(self) -> dict | None:
        return self.info.source

    @property
    def local(self) -> bool:
        """On this computer only: not published on GitHub."""
        return self.remote is None


def git_origin(path: Path) -> str | None:
    """The origin URL, read from .git/config (no git command)."""
    try:
        text = (path / ".git" / "config").read_text()
    except OSError:
        return None
    return dict(remotes(text)).get("origin")


def read(vault_id: str, path: Path) -> Vault:
    try:
        text = (path / VAULT_FILE).read_text()
    except OSError:
        text = ""
    info, problems = parse_vault_toml(text)
    return Vault(vault_id, path, git_origin(path), info, problems)


def path_of(vault_id: str) -> Path:
    owner, repo = vault_id.split("/", 1)
    return vaults_dir() / owner / repo


def _dirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def joined() -> list[str]:
    """Owners you joined: each has a folder in vaults/."""
    return [p.name for p in _dirs(vaults_dir()) if re.fullmatch(OWNER_RE, p.name)]


def on_disk() -> dict[str, Vault]:
    """Every vault in vaults/OWNER/REPO: a git repo there is a vault."""
    out = {}
    for owner in _dirs(vaults_dir()):
        for repo in _dirs(owner):
            vault_id = f"{owner.name}/{repo.name}"
            if (repo / ".git").exists() and valid_id(vault_id):
                out[vault_id] = read(vault_id, repo)
    return out
