"""Shared test fixtures: a fake world for hook tests, a fake GitHub, and a fake computer."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ME = "sam"
# short name -> (vault ID, who can see it)
VAULTS = {
    "sam-personal": ("sam/vault-sam-personal", {"kind": "me"}),
    "sam-side": ("sam/vault-side", {"kind": "people", "logins": ["sam"]}),
    "acme-founders": ("acme/vault-founders", {"kind": "people", "logins": ["sam", "Lee"]}),
    "acme-everyone": ("acme/vault-everyone", {"kind": "people", "logins": ["sam", "Lee", "ana"]}),
    "acme-slack": ("acme/vault-slack", {"kind": "people", "logins": ["sam", "Lee", "ana"]}),
    "acme-handbook": ("acme/vault-handbook", {"kind": "unknown", "reason": "you have read-only access"}),
    "acme-docs": ("acme/vault-docs", {"kind": "everyone"}),
    "acme-sam-personal": ("acme/vault-sam-personal", {"kind": "me"}),
    "acme-drive": ("acme/vault-drive", {"kind": "people", "logins": ["sam", "Lee"]}),
    "sam-recipes": ("sam/vault-recipes", {"kind": "me"}),  # on this computer only
}
ABOUT = {"acme-everyone": "Notes everyone at Acme can see.", "acme-founders": "Founders' notes: fundraising, hiring.",
         "acme-drive": "The text of every file in the Acme shared drive. Claude only reads it.",
         "sam-personal": "sam's personal notes."}


def git_repo(path: Path, *urls: str) -> Path:
    """A folder that looks like a git clone to vl: a .git/config with these remotes, origin first."""
    (path / ".git").mkdir(parents=True)
    text = "[core]\n\tbare = false\n"
    for i, url in enumerate(urls):
        text += f'[remote "{"origin" if i == 0 else f"r{i}"}"]\n\turl = {url}\n'
    (path / ".git" / "config").write_text(text)
    return path


class World:
    """sam joined acme (and has their own account). Code repos are cloned in different places."""

    def __init__(self, root: Path):
        self.home = root / "home"
        self.vl = self.home / ".vaultlines"
        self.vaults = self.vl / "vaults"
        code = self.home / "code"
        self.site = git_repo(code / "site", "git@github.com:acme/site.git")  # notes_from: everyone
        self.api = self.site / "api"  # a folder in the site repo
        self.app = git_repo(self.home / "elsewhere" / "app-clone", "https://github.com/acme/app.git")  # everyone
        self.legal = git_repo(code / "legal", "https://github.com/acme/legal")  # notes_from: founders
        self.sheety = git_repo(code / "sheety", "https://github.com/acme/sheety.git")  # in no notes_from
        self.both = git_repo(code / "both", "https://github.com/acme/both.git")  # in two notes_from
        self.blog = git_repo(code / "blog", "https://github.com/sam/blog.git")  # sam's own account
        self.oss = git_repo(code / "linux", "https://github.com/torvalds/linux.git")  # an owner sam didn't join
        self.desktop = self.home / "Desktop"
        self.writing = self.home / "writing"  # a [folders] entry
        for d in (self.api, self.desktop, self.writing):
            d.mkdir(parents=True)
        self.ids = {short: vid for short, (vid, _) in VAULTS.items()}
        for vid in self.ids.values():
            (self.vaults / vid).mkdir(parents=True)
        self.fetch = self.vl / "cache" / "fetch" / "acme" / "vault-drive"
        acme = sorted(s for s, vid in self.ids.items() if vid.startswith("acme/"))
        self.runtime = {
            "version": 4,
            "written_at": 0,
            "config": str(self.vl / "config.toml"),
            "me": ME,
            "on_leak": "ask",
            "vaults": {short: {"id": vid, "paths": [str(self.vaults / vid)], "show": f"~/.vaultlines/vaults/{vid}",
                               "about": ABOUT.get(short, ""),
                               "audience": {"logins": [], "reason": "", **copy.deepcopy(aud)}}
                       for short, (vid, aud) in VAULTS.items()},
            "owners": {
                "acme": {"personal": "acme-sam-personal", "vaults": acme,
                         "notes_from": {"acme/site": "acme-everyone", "acme/app": "acme-everyone",
                                        "acme/legal": "acme-founders"},
                         "conflicts": {"acme/both": ["acme-everyone", "acme-founders"]}},
                "sam": {"personal": "sam-personal", "vaults": ["sam-personal", "sam-recipes", "sam-side"], "notes_from": {},
                        "conflicts": {}},
            },
            "repos": {},
            "folders": {str(self.writing): {"writes": "sam-recipes", "reads": []}},
            "default": {"writes": "sam-personal", "reads": []},
            "plugins": {
                "basic-memory": {
                    "kind": "basic-memory",
                    "tool_prefixes": ["mcp__basic-memory__"],
                    "data": {"plugin": True, "projects": {**{s: s for s in VAULTS}, "main": None}},
                },
            },
        }
        self.runtime["vaults"]["acme-drive"].update(source="gdrive", fetch=[str(self.fetch)])

    def vault(self, short: str, *rest: str) -> str:
        return str(self.vaults.joinpath(self.ids[short], *rest))


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    w = World(tmp_path.resolve())
    monkeypatch.setenv("HOME", str(w.home))
    monkeypatch.setenv("VAULTLINES_HOME", str(w.vl))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("VAULTLINES_TEST_REMOTES", raising=False)
    return w


def write_runtime(world: World) -> None:
    from vaultlines.hook import runtime_path

    runtime_path().parent.mkdir(parents=True, exist_ok=True)
    runtime_path().write_text(json.dumps(world.runtime))


# ---------------------------------------------------------------- a fake GitHub, and a fake computer

def push_repo(root: Path, repo_id: str, files: dict[str, str]) -> Path:
    """A bare repo at root/OWNER/REPO.git with these files committed (none: empty)."""
    import subprocess

    bare = root / f"{repo_id}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    if files:
        work = root / ".work" / repo_id
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
        commit_files(work, files, "seed")
    return bare


def commit_files(work: Path, files: dict[str, str], message: str) -> None:
    import subprocess

    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", message], check=True)
    subprocess.run([*git, "push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)


class FakeGitHub:
    """VAULTLINES_FAKE_GITHUB: a JSON file of orgs and repos (who can push or read each),
    with bare repos under `root`. VAULTLINES_FAKE_LOGIN says who you are."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path / "remotes"
        self.root.mkdir()
        self.path = tmp_path / "github.json"
        self.monkeypatch = monkeypatch
        self.save({"root": str(self.root), "orgs": {}, "repos": {}})
        monkeypatch.setenv("VAULTLINES_FAKE_GITHUB", str(self.path))
        monkeypatch.setenv("VAULTLINES_TEST_REMOTES", "1")
        self.login("alice")

    def load(self) -> dict:
        return json.loads(self.path.read_text())

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data))

    def login(self, who: str) -> None:
        self.monkeypatch.setenv("VAULTLINES_FAKE_LOGIN", who)

    def org(self, name: str, members: list[str]) -> None:
        data = self.load()
        data["orgs"][name] = members
        self.save(data)

    def repo(self, repo_id: str, access, files: dict[str, str] | None = None) -> Path:
        """`access`: {"push": [...], "read": [...]}, a list (all push), or "public"."""
        data = self.load()
        data["repos"][repo_id] = {"push": access} if isinstance(access, list) else access
        self.save(data)
        return push_repo(self.root, repo_id, files or {})

    def vault(self, repo_id: str, access, toml: str = 'about = "x"\n') -> Path:
        return self.repo(repo_id, access, {"vault.toml": toml})

    def url(self, repo_id: str) -> str:
        return f"file://{self.root}/{repo_id}.git"


@pytest.fixture
def fake_github(tmp_path, monkeypatch) -> FakeGitHub:
    return FakeGitHub(tmp_path, monkeypatch)


@pytest.fixture
def computer(tmp_path, monkeypatch):
    """A fake computer: its own home, vl home and Claude settings. Nothing outside tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VAULTLINES_HOME", str(home / ".vaultlines"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("VAULTLINES_NO_LAUNCHD", "1")
    monkeypatch.setenv("VAULTLINES_NO_NOTIFY", "1")
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[init]\n\tdefaultBranch = main\n[user]\n\tname = t\n\temail = t@t\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    from vaultlines import config

    config.write_template(basic_memory=False)  # no uvx in tests
    return home


def vl(*args: str) -> int:
    """Run `vl ARGS` in this process. Returns the exit code."""
    from vaultlines import cli

    try:
        cli.main(list(args))
    except SystemExit as e:
        return int(e.code or 0)
    return 0
