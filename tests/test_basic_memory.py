"""The Basic Memory plugin: which projects a call uses, and what it refuses."""

import json

import pytest

from vaultlines import plugins
from vaultlines.plugins import basic_memory as bm
from vaultlines.plugins.basic_memory import READS, WRITES, resolve

PROJECTS = ["acme-founders", "acme-everyone", "Personal Notes", "main"]


def test_project_given():
    a = resolve("read_note", {"identifier": "x", "project": "acme-everyone"}, PROJECTS, "acme-founders")
    assert (a.kind, a.projects, a.block, a.updated_input) == ("read", ["acme-everyone"], None, None)


def test_project_names_match_like_basic_memory():
    a = resolve("read_note", {"identifier": "x", "project": "personal_notes"}, PROJECTS, None)
    assert a.projects == ["Personal Notes"]


def test_project_missing_is_filled_in():
    a = resolve("search_notes", {"query": "x"}, PROJECTS, "acme-founders")
    assert a.projects == ["acme-founders"]
    assert a.updated_input == {"query": "x", "project": "acme-founders"}
    a = resolve("write_note", {"title": "t", "content": "c", "directory": "d", "project": None}, PROJECTS, "main")
    assert a.kind == "write"
    assert a.updated_input["project"] == "main"


def test_project_missing_and_no_folder():
    assert "No vaults" in resolve("search_notes", {"query": "x"}, PROJECTS, None).block


@pytest.mark.parametrize("tool, args, message", [
    ("read_note", {"identifier": "x", "project_id": "123"}, "project_id"),
    ("write_note", {"title": "t", "content": "c", "directory": "d", "workspace": "w"}, "workspace"),
    ("search_notes", {"query": "x", "search_all_projects": True}, "every project"),
    ("search_notes", {"query": "x", "all_projects": True}, "every project"),
    ("recent_activity", {}, "every vault"),
    ("recent_activity", {"timeframe": "7d"}, "every vault"),
    ("read_note", {"identifier": "x", "sneaky": 1}, "sneaky"),
    ("read_note", {"identifier": "x", "project": "ws/acme-everyone"}, "plain vault name"),
    ("search", {"query": "x"}, "search_notes"),
    ("fetch", {"id": "x"}, "read_note"),
    ("create_memory_project", {"project_name": "x", "project_path": "/tmp"}, "vl vault"),
    ("delete_project", {"project_name": "main"}, "vl vault"),
    ("brand_new_tool", {}, "doesn't know"),
])
def test_blocked(tool, args, message):
    a = resolve(tool, args, PROJECTS, "acme-founders")
    assert a.block and message in a.block


def test_search_all_projects_false_is_fine():
    a = resolve("search_notes", {"query": "x", "search_all_projects": False, "project": "main"}, PROJECTS, None)
    assert a.block is None


def test_aliases_are_known():
    assert resolve("search_notes", {"q": "x", "limit": 5}, PROJECTS, "main").block is None
    assert resolve("move_note", {"identifier": "a", "to": "b"}, PROJECTS, "main").block is None


def test_memory_urls_route_to_their_project():
    a = resolve("build_context", {"url": "memory://acme-founders/plans/*", "project": "acme-everyone"}, PROJECTS, None)
    assert a.projects == ["acme-everyone", "acme-founders"]
    a = resolve("read_note", {"identifier": "memory://notes/x", "project": "acme-everyone"}, PROJECTS, None)
    assert a.projects == ["acme-everyone"]  # "notes" isn't a project, so it's a folder
    a = resolve("read_note", {"identifier": "memory://main"}, PROJECTS, "acme-everyone")
    assert a.projects == ["acme-everyone"]  # one segment: a note, not a project
    a = resolve("edit_note", {"identifier": "memory://Main/x", "operation": "append", "content": "c",
                              "project": "acme-everyone"}, PROJECTS, None)
    assert (a.kind, a.projects) == ("write", ["acme-everyone", "main"])


def test_every_tool_knows_its_project_free_arguments():
    for table in (READS, WRITES):
        for tool, args in table.items():
            assert "project" not in args and "project_id" not in args, tool


# ---------------------------------------------------------------- the plugin, as the hook sees it

DATA = {"plugin": True, "projects": {"acme-founders": "acme-founders", "Personal Notes": "personal", "main": None}}
ACME = {"writes": "acme-founders", "reads": ["personal"]}


def test_registry():
    assert plugins.KINDS["basic-memory"] is bm
    assert plugins.TOOL_PREFIXES == ("mcp__basic-memory__",)


def test_plugin_for_tool():
    runtime = {"plugins": {"basic-memory": {"kind": "basic-memory", "tool_prefixes": ["mcp__basic-memory__"],
                                            "data": DATA}}}
    assert plugins.plugin_for_tool(runtime, "mcp__basic-memory__read_note") == (bm, DATA)
    assert plugins.plugin_for_tool(runtime, "Read") is None
    assert plugins.plugin_for_tool({"plugins": {}}, "mcp__basic-memory__read_note") is None


def test_on_call_reports_vaults_not_projects():
    a = bm.on_call("mcp__basic-memory__read_note", {"identifier": "x", "project": "personal_notes"}, ACME, DATA)
    assert (a.kind, a.vaults, a.block, a.updated_input) == ("read", ["personal"], None, None)


def test_on_call_fills_in_the_sessions_writes_project():
    a = bm.on_call("mcp__basic-memory__write_note", {"title": "t", "content": "c", "directory": "d"}, ACME, DATA)
    assert (a.kind, a.vaults) == ("write", ["acme-founders"])
    assert a.updated_input["project"] == "acme-founders"


def test_on_call_blocks_a_project_that_isnt_a_vault():
    for project in ("main", "nope"):
        a = bm.on_call("mcp__basic-memory__read_note", {"identifier": "x", "project": project}, ACME, DATA)
        assert a.block == f"Basic Memory project '{project}' isn't a vault vl knows. This session uses acme-founders, personal."
    a = bm.on_call("mcp__basic-memory__read_note", {"identifier": "x", "project": "main"}, None, DATA)
    assert a.block.endswith("isn't a vault vl knows. No vaults are set up here.")


def test_on_call_passes_resolve_blocks_through():
    a = bm.on_call("mcp__basic-memory__fetch", {"id": "x"}, ACME, DATA)
    assert "read_note" in a.block and a.vaults == []


def test_context_vaults_follow_the_plugin_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"basicMemory": {"primaryProject": "personal-notes", "secondaryProjects": ["main", "acme-founders"]}}))
    assert bm.context_vaults(str(tmp_path), DATA) == ["personal", "acme-founders"]  # "main" isn't a vault
    assert bm.context_vaults(str(tmp_path), {**DATA, "plugin": False}) == []


def test_briefing_text():
    assert bm.briefing("acme", DATA) == ('Basic Memory project="acme"',
                                         'Always pass project="..." to Basic Memory tools.')


@pytest.mark.parametrize("settings, problem", [
    ({}, None),
    ({"command": "uvx basic-memory"}, None),
    ({"command": ""}, "command"),
    ({"command": 3}, "command"),
])
def test_validate(settings, problem):
    result = bm.validate(settings)
    assert (result[0] if result else None) == problem


# ---------------------------------------------------------------- the block in a repo, at session start

RUNTIME = {"default": {"writes": "kabir-personal", "reads": []}}
BM_DATA = {"plugin": True, "projects": {"acme-public": "acme-public", "kabir-personal": "kabir-personal"}}


def _block(path):
    return json.loads((path / ".claude" / "settings.local.json").read_text()).get("basicMemory")


def _repo(tmp_path, name="marketing"):
    import subprocess

    repo = tmp_path / name
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_session_start_points_the_plugin_at_the_repos_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    rules = {"writes": "acme-public", "reads": [], "how": "notes_from", "root": str(repo), "folder": None}
    bm.session_start(rules, RUNTIME, BM_DATA)
    assert _block(repo) == bm.plugin_block("acme-public")
    excluded = (repo / ".git" / "info" / "exclude").read_text()
    assert ".claude/settings.local.json" in excluded
    before = (repo / ".claude" / "settings.local.json").stat().st_mtime_ns
    bm.session_start(rules, RUNTIME, BM_DATA)  # already right: not written again
    assert (repo / ".claude" / "settings.local.json").stat().st_mtime_ns == before


def test_session_start_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"model": "opus"}))
    bm.session_start({"writes": "acme-public", "how": "notes_from", "root": str(repo)}, RUNTIME, BM_DATA)
    data = json.loads((repo / ".claude" / "settings.local.json").read_text())
    assert data["model"] == "opus" and data["basicMemory"]["primaryProject"] == "acme-public"


def test_session_start_removes_vls_block_where_the_default_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    bm.session_start({"writes": "acme-public", "how": "notes_from", "root": str(repo)}, RUNTIME, BM_DATA)
    bm.session_start({"writes": "kabir-personal", "how": "personal", "root": str(repo)}, RUNTIME, BM_DATA)
    assert _block(repo) is None
    # A block someone wrote by hand stays.
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"basicMemory": {"primaryProject": "mine"}}))
    bm.session_start({"writes": "kabir-personal", "how": "personal", "root": str(repo)}, RUNTIME, BM_DATA)
    assert _block(repo) == {"primaryProject": "mine"}


def test_session_start_leaves_places_without_a_repo_or_folder_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bm.session_start({"writes": "acme-public", "how": "default", "root": None, "folder": None}, RUNTIME, BM_DATA)
    bm.session_start({"writes": "acme-public", "how": "notes_from", "root": str(tmp_path / "x")},
                     RUNTIME, {**BM_DATA, "plugin": False})
    assert not (tmp_path / "x").exists()


def test_session_start_uses_a_folder_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    folder = tmp_path / "writing"
    folder.mkdir()
    bm.session_start({"writes": "acme-public", "how": "folder", "root": None, "folder": str(folder)},
                     RUNTIME, BM_DATA)
    assert _block(folder)["primaryProject"] == "acme-public"


def test_session_start_skips_a_vault_basic_memory_doesnt_know_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    bm.session_start({"writes": "acme-new", "how": "notes_from", "root": str(repo)}, RUNTIME, BM_DATA)
    assert not (repo / ".claude").exists()


def test_apply_before_init_leaves_the_user_level_block_alone(tmp_path, monkeypatch):
    from vaultlines import claude
    from vaultlines.config import Config

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(bm, "projects", lambda settings: {})
    monkeypatch.setattr(claude, "user_servers", lambda: {bm.SERVER: {"command": "uvx", "args": ["basic-memory", "mcp"]}})
    path = claude.plugin_user_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"basicMemory": {"primaryProject": "personal"}}))
    state = bm.apply(Config(), {"kind": "basic-memory"}, {}, [])
    assert json.loads(path.read_text())["basicMemory"] == {"primaryProject": "personal"}
    assert state["blocks"] == []
