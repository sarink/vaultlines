"""The Basic Memory adapter: which projects a call uses, and what it refuses."""

import pytest

from vaultlines.adapters.basic_memory import READS, WRITES, resolve

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
