"""rules.py: which vaults a session uses, from the repo it starts in."""

from __future__ import annotations

import subprocess

import pytest

from vaultlines import rules
from vaultlines.rules import remotes, resolve

CONFIG = """\
[core]
\trepositoryformatversion = 0
[remote "upstream"]
\turl = https://github.com/mixim-ai/marketing.git
\tfetch = +refs/heads/*:refs/remotes/upstream/*
[remote "origin"]
\turl = "git@github.com:kabir/marketing.git" ; my fork
[branch "main"]
\tremote = origin
"""


def test_remotes_origin_first():
    assert remotes(CONFIG) == [("origin", "git@github.com:kabir/marketing.git"),
                               ("upstream", "https://github.com/mixim-ai/marketing.git")]


def test_remotes_odd_spacing_and_case():
    assert remotes('[remote "o"]\n  URL=https://github.com/a/b\n[Remote "p"] url = x') == [
        ("o", "https://github.com/a/b"), ("p", "x")]
    assert remotes("") == []


@pytest.mark.parametrize("url, found", [
    ("https://github.com/Mixim-AI/Marketing.git", "mixim-ai/marketing"),
    ("git@github.com:mixim-ai/marketing", "mixim-ai/marketing"),
    ("ssh://git@github.com/mixim-ai/marketing.git/", "mixim-ai/marketing"),
    ("https://gitlab.com/mixim-ai/marketing.git", None),
    ("/srv/git/marketing.git", None),
])
def test_repo_id(url, found):
    assert rules.repo_id(url) == found


# ---------------------------------------------------------------- finding the repo

def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def clone_at(path, *urls):
    """A repo at `path` with these remotes: the first is origin."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", str(path))
    for i, url in enumerate(urls):
        git("-C", str(path), "remote", "add", "origin" if i == 0 else f"r{i}", url)
    return path


def test_repo_of_a_subfolder(tmp_path):
    repo = clone_at(tmp_path / "marketing", "https://github.com/mixim-ai/marketing.git")
    (repo / "src" / "deep").mkdir(parents=True)
    assert rules.repo_of(str(repo / "src" / "deep")) == (str(repo.resolve()), ["mixim-ai/marketing"])


def test_no_repo(tmp_path):
    assert rules.repo_of(str(tmp_path)) is None


def test_repo_without_remotes(tmp_path):
    repo = clone_at(tmp_path / "x")
    assert rules.repo_of(str(repo)) == (str(repo.resolve()), [])


def test_a_worktree_reads_the_main_repos_config(tmp_path):
    repo = clone_at(tmp_path / "main", "https://github.com/mixim-ai/marketing.git")
    git("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git("-C", str(repo), "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature")
    assert rules.repo_of(str(tmp_path / "wt")) == (str((tmp_path / "wt").resolve()), ["mixim-ai/marketing"])


def test_a_gitdir_file_that_points_elsewhere(tmp_path):
    real = clone_at(tmp_path / "store" / "repo", "https://github.com/mixim-ai/studio.git")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {real / '.git'}\n")
    assert rules.repo_of(str(work)) == (str(work.resolve()), ["mixim-ai/studio"])


# ---------------------------------------------------------------- resolving

def runtime(**extra):
    rt = {
        "owners": {
            "mixim-ai": {"personal": "mixim-ai-kabir-personal",
                         "vaults": ["mixim-ai-hq", "mixim-ai-kabir-personal", "mixim-ai-private", "mixim-ai-public"],
                         "notes_from": {"mixim-ai/marketing": "mixim-ai-public",
                                        "mixim-ai/jorge-ip-theft": "mixim-ai-private"},
                         "conflicts": {"mixim-ai/both": ["mixim-ai-private", "mixim-ai-public"]}},
            "kabir": {"personal": "kabir-personal", "vaults": ["kabir-personal", "kabir-side"],
                      "notes_from": {}, "conflicts": {}},
        },
        "repos": {}, "folders": {}, "default": {"writes": "kabir-personal", "reads": []},
    }
    rt.update(extra)
    return rt


def at(tmp_path, url, name="clone"):
    return str(clone_at(tmp_path / name, url))


def test_a_repo_in_notes_from_writes_there_and_reads_the_owners_other_vaults(tmp_path):
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/marketing.git"), runtime())
    assert r["writes"] == "mixim-ai-public"
    assert r["reads"] == ["mixim-ai-hq", "mixim-ai-kabir-personal", "mixim-ai-private"]
    assert (r["how"], r["repo"]) == ("notes_from", "mixim-ai/marketing")


def test_the_same_repo_cloned_anywhere_gets_the_same_rules(tmp_path):
    a = resolve(at(tmp_path, "https://github.com/mixim-ai/marketing.git", "a"), runtime())
    b = resolve(at(tmp_path, "git@github.com:mixim-ai/marketing.git", "deep/b"), runtime())
    assert (a["writes"], a["reads"]) == (b["writes"], b["reads"])


def test_a_repo_in_no_notes_from_writes_to_your_personal_vault_for_the_owner(tmp_path):
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/sheety.git"), runtime())
    assert (r["writes"], r["how"]) == ("mixim-ai-kabir-personal", "personal")
    assert r["reads"] == ["mixim-ai-hq", "mixim-ai-private", "mixim-ai-public"]


def test_your_own_account(tmp_path):
    r = resolve(at(tmp_path, "https://github.com/kabir/blog.git"), runtime())
    assert (r["writes"], r["reads"]) == ("kabir-personal", ["kabir-side"])


def test_no_repo_writes_to_your_personal_vault_and_reads_nothing(tmp_path):
    r = resolve(str(tmp_path), runtime())
    assert (r["writes"], r["reads"], r["how"], r["repo"]) == ("kabir-personal", [], "default", None)


def test_a_repo_of_an_owner_you_didnt_join_is_like_no_repo(tmp_path):
    r = resolve(at(tmp_path, "https://github.com/torvalds/linux.git"), runtime())
    assert (r["writes"], r["reads"], r["how"]) == ("kabir-personal", [], "default")


def test_a_repo_without_a_github_remote(tmp_path):
    r = resolve(at(tmp_path, "https://gitlab.com/mixim-ai/marketing.git"), runtime())
    assert r["how"] == "default"


def test_owners_stay_apart(tmp_path):
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/marketing.git"), runtime())
    assert not {"kabir-personal", "kabir-side"} & {r["writes"], *r["reads"]}


def test_a_fork_follows_origin_first(tmp_path):
    fork = clone_at(tmp_path / "fork", "git@github.com:kabir/marketing.git", "https://github.com/mixim-ai/marketing.git")
    assert resolve(str(fork), runtime())["writes"] == "kabir-personal"
    # origin's owner isn't joined: the next remote counts.
    other = clone_at(tmp_path / "other", "git@github.com:someone/marketing.git", "https://github.com/mixim-ai/marketing.git")
    assert resolve(str(other), runtime())["writes"] == "mixim-ai-public"


def test_two_vaults_claiming_a_repo_send_its_notes_to_your_personal_vault(tmp_path):
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/both.git"), runtime())
    assert (r["writes"], r["how"]) == ("mixim-ai-kabir-personal", "conflict")
    assert r["conflict"] == ["mixim-ai-private", "mixim-ai-public"]


def test_a_repos_entry_wins(tmp_path):
    rt = runtime(repos={"mixim-ai/marketing": {"writes": "mixim-ai-private", "reads": ["local-recipes"]}})
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/marketing.git"), rt)
    assert (r["writes"], r["how"]) == ("mixim-ai-private", "repos")
    assert r["reads"] == ["mixim-ai-hq", "mixim-ai-kabir-personal", "mixim-ai-public", "local-recipes"]


def test_a_repos_entry_with_only_reads_adds_them(tmp_path):
    rt = runtime(repos={"mixim-ai/marketing": {"writes": None, "reads": ["local-recipes"]}})
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/marketing.git"), rt)
    assert (r["writes"], r["how"]) == ("mixim-ai-public", "notes_from")
    assert r["reads"][-1] == "local-recipes"


def test_an_owner_wide_entry_adds_reads_to_every_repo(tmp_path):
    rt = runtime(repos={"mixim-ai/*": {"writes": None, "reads": ["kabir-side"]},
                        "mixim-ai/marketing": {"writes": None, "reads": ["local-recipes"]}})
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/marketing.git"), rt)
    assert r["reads"][-2:] == ["kabir-side", "local-recipes"]
    r = resolve(at(tmp_path, "https://github.com/mixim-ai/sheety.git", "sheety"), rt)
    assert r["reads"][-1] == "kabir-side"
    rt["repos"]["mixim-ai/*"]["writes"] = "mixim-ai-public"
    assert resolve(at(tmp_path, "https://github.com/mixim-ai/sheety.git", "sheety2"), rt)["writes"] == "mixim-ai-public"


def test_a_folders_entry_counts_outside_joined_repos(tmp_path):
    writing = tmp_path / "writing"
    (writing / "drafts").mkdir(parents=True)
    rt = runtime(folders={str(writing.resolve()): {"writes": "local-recipes", "reads": ["kabir-side"]}})
    r = resolve(str(writing / "drafts"), rt)
    assert (r["writes"], r["reads"], r["how"], r["folder"]) == ("local-recipes", ["kabir-side"], "folder",
                                                                 str(writing.resolve()))
    # A folder entry without writes uses your personal vault.
    rt["folders"][str(writing.resolve())]["writes"] = None
    assert resolve(str(writing), rt)["writes"] == "kabir-personal"


def test_a_folders_entry_doesnt_count_in_a_joined_repo(tmp_path):
    repo = clone_at(tmp_path / "code" / "marketing", "https://github.com/mixim-ai/marketing.git")
    rt = runtime(folders={str((tmp_path / "code").resolve()): {"writes": "local-recipes", "reads": []}})
    assert resolve(str(repo), rt)["writes"] == "mixim-ai-public"


def test_the_root_is_the_repos_top_folder(tmp_path):
    repo = clone_at(tmp_path / "marketing", "https://github.com/mixim-ai/marketing.git")
    (repo / "src").mkdir()
    assert resolve(str(repo / "src"), runtime())["root"] == str(repo.resolve())
    assert resolve(str(tmp_path), runtime())["root"] is None
