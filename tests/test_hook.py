"""decide(): the hook's rules, one scenario at a time."""

from __future__ import annotations

import os

from vaultlines import label as lbl
from vaultlines.hook import bash_vaults, decide, vault_of
from vaultlines.plugins.basic_memory import plugin_projects


class Session:
    """A fake Claude session: feeds decide() events and keeps its state, like `vl hook` does."""

    def __init__(self, world, folder, source="startup", briefing=(), state=None, runtime=None):
        self.world = world
        self.runtime = runtime or world.runtime
        self.folder = str(folder)
        self.state = state
        self.last = self.event({"hook_event_name": "SessionStart", "source": source}, briefing)

    def event(self, event, briefing=()):
        event = {"session_id": "s1", "cwd": self.folder, **event}
        out, new = decide(event, self.runtime, self.state, self.folder, briefing)
        if new is not None:
            self.state = new
        return out

    def call(self, tool, **tool_input):
        self.last = self.event({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})
        return self.last

    def bm(self, tool, **tool_input):
        return self.call(f"mcp__basic-memory__{tool}", **tool_input)

    @property
    def label(self):
        return lbl.from_json(self.state["label"])


def decision(out):
    if out is None:
        return None
    return out["hookSpecificOutput"].get("permissionDecision")


def reason(out):
    return out["hookSpecificOutput"].get("permissionDecisionReason", "")


# ---------------------------------------------------------------- the scenario table

def test_1_focus_blocks_a_vault_the_folder_doesnt_use(world):
    s = Session(world, world.blog)  # not listed: "~" applies
    out = s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    assert decision(out) == "deny"
    assert "`acme-founders` is not used in this folder" in reason(out)
    assert "personal, docs" in reason(out)


def test_2_subfolder_uses_its_closest_listed_parent(world):
    s = Session(world, world.api)
    ctx = s.last["hookSpecificOutput"]["additionalContext"]
    assert "`acme-founders` vault" in ctx
    assert "acme-everyone, acme-slack" in ctx
    assert s.state["folder"] == str(world.acme)


def test_3_same_audience_read_then_write_is_allowed(world):
    s = Session(world, world.site)
    assert decision(s.call("Read", file_path=world.vault("acme-slack", "general.md"))) is None
    assert decision(s.call("Write", file_path=world.vault("acme-everyone", "note.md"), content="x")) is None


def test_4_narrower_read_then_write_asks_and_names_the_new_people(world):
    s = Session(world, world.acme)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Write", file_path=world.vault("acme-everyone", "note.md"), content="x")
    assert decision(out) == "ask"
    assert "This session read acme-founders." in reason(out)
    assert "ana would see this in acme-everyone" in reason(out)


def test_5_wider_read_then_narrower_write_is_allowed(world):
    s = Session(world, world.acme)
    s.call("Read", file_path=world.vault("acme-everyone", "a.md"))
    assert decision(s.call("Edit", file_path=world.vault("acme-founders", "b.md"))) is None


def test_6_reading_a_vault_only_you_see_makes_shared_writes_ask(world):
    s = Session(world, world.side_project)
    s.call("Read", file_path=world.vault("side", "idea.md"))
    assert s.label == lbl.ONLY_YOU
    out = s.call("Write", file_path=world.vault("acme-founders", "x.md"))
    assert decision(out) == "ask"
    assert "Lee would see this" in reason(out)


def test_7_other_tools_dont_change_the_label(world):
    s = Session(world, world.site)
    before = dict(s.state)
    assert s.call("WebFetch", url="https://example.com") is None
    assert s.call("mcp__claude_ai_Gmail__get_thread", id="1") is None
    assert s.state == before
    assert decision(s.call("Write", file_path=world.vault("acme-everyone", "n.md"))) is None


def test_9_a_new_person_on_github_makes_scenario_3_ask(world):
    world.runtime["vaults"]["acme-everyone"]["audience"]["logins"].append("contractor")
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-slack", "general.md"))
    out = s.call("Write", file_path=world.vault("acme-everyone", "note.md"))
    assert decision(out) == "ask"
    assert "contractor would see this" in reason(out)


# ---------------------------------------------------------------- label rules

def test_on_leak_block_denies(world):
    world.runtime["on_leak"] = "block"
    s = Session(world, world.acme)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Write", file_path=world.vault("acme-everyone", "note.md"))
    assert decision(out) == "deny"
    assert 'on_leak = "block"' in reason(out)


def test_writing_a_reads_vault_always_asks(world):
    s = Session(world, world.acme)
    out = s.call("Write", file_path=world.vault("acme-slack", "x.md"))
    assert decision(out) == "ask"
    assert "`acme-slack` is a read vault in this folder" in reason(out)


def test_unknown_audience_read_counts_as_only_you(world):
    s = Session(world, world.side_project)
    s.call("Grep", pattern="x", path=world.vault("handbook"))
    assert s.label == lbl.ONLY_YOU
    assert s.state["read"] == ["handbook"]


def test_public_read_changes_nothing(world):
    s = Session(world, world.home)
    s.call("Read", file_path=world.vault("docs", "readme.md"))
    assert s.label is lbl.EVERYONE
    assert s.state["read"] == ["docs"]


def test_writing_an_unknown_audience_vault_after_a_restricted_read_asks(world):
    world.runtime["folders"][str(world.side_project)]["writes"] = "handbook"
    world.runtime["folders"][str(world.side_project)]["reads"] = ["side"]
    s = Session(world, world.side_project)
    s.call("Read", file_path=world.vault("side", "x.md"))
    out = s.call("Write", file_path=world.vault("handbook", "x.md"))
    assert decision(out) == "ask"
    assert "people vl can't list" in reason(out)


def test_only_you_vault_takes_any_write(world):
    world.runtime["folders"][str(world.side_project)]["writes"] = "side"
    world.runtime["folders"][str(world.side_project)]["reads"] = ["acme-founders"]
    s = Session(world, world.side_project)
    s.call("Read", file_path=world.vault("acme-founders", "x.md"))
    assert decision(s.call("Write", file_path=world.vault("side", "x.md"))) is None


def test_denied_calls_dont_count_as_reads(world):
    s = Session(world, world.blog)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    assert s.state["read"] == []
    assert s.label is lbl.EVERYONE


def test_asked_calls_count_as_reads(world):
    s = Session(world, world.acme)
    s.call("Bash", command=f"cat {world.vault('acme-slack', 'x.md')}")
    assert decision(s.last) == "ask"
    assert s.state["read"] == ["acme-slack"]


def test_login_case_doesnt_matter(world):
    world.runtime["me"] = "SAM"
    world.runtime["vaults"]["acme-founders"]["audience"]["logins"] = ["Sam", "lee"]
    s = Session(world, world.acme)
    s.call("Read", file_path=world.vault("acme-everyone", "a.md"))
    assert decision(s.call("Write", file_path=world.vault("acme-founders", "b.md"))) is None


# ---------------------------------------------------------------- sessions

def test_briefing_counts_as_a_read(world):
    s = Session(world, world.acme, briefing=["acme-founders"])
    assert s.state["read"] == ["acme-founders"]
    out = s.call("Write", file_path=world.vault("acme-everyone", "n.md"))
    assert decision(out) == "ask"


def test_fork_starts_as_only_you(world):
    s = Session(world, world.site, source="fork")
    assert s.label == lbl.ONLY_YOU
    out = s.call("Write", file_path=world.vault("acme-everyone", "n.md"))
    assert decision(out) == "ask"
    assert "because it was forked" in reason(out)


def test_resume_keeps_the_label(world):
    s = Session(world, world.acme)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    resumed = Session(world, world.acme, source="resume", state=s.state)
    assert resumed.state["read"] == ["acme-founders"]
    assert resumed.label == s.label


def test_resume_elsewhere_keeps_the_original_folder(world):
    s = Session(world, world.site)
    resumed = Session(world, world.blog, source="resume", state=s.state)
    assert resumed.state["folder"] == str(world.site)
    assert "`acme-everyone` vault" in resumed.last["hookSpecificOutput"]["additionalContext"]


def test_resume_or_compact_without_a_record_starts_as_only_you(world):
    for source in ("resume", "compact"):
        s = Session(world, world.site, source=source)
        assert s.label == lbl.ONLY_YOU
        assert "no record" in s.state["why"]


def test_tool_call_without_a_session_record_is_only_you(world):
    out, _ = decide({"hook_event_name": "PreToolUse", "session_id": "x", "cwd": str(world.site),
                         "tool_name": "Write", "tool_input": {"file_path": world.vault("acme-everyone", "n.md")}},
                        world.runtime, None, str(world.site))
    assert decision(out) == "ask"
    assert "no record of how it started" in reason(out)


def test_clear_starts_fresh(world):
    s = Session(world, world.acme)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    cleared = Session(world, world.acme, source="clear", state=s.state)
    assert cleared.label is lbl.EVERYONE
    assert cleared.state["read"] == []


def test_the_folder_is_where_the_session_started(world):
    s = Session(world, world.site)
    s.folder = str(world.blog)  # Claude cd'd somewhere else; the tool call's cwd changes
    assert decision(s.call("Read", file_path=world.vault("acme-slack", "x.md"))) is None


def test_unlisted_folder_outside_home_has_no_vaults(world, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    s = Session(world, elsewhere)
    assert s.last is None  # no context added
    out = s.call("Read", file_path=world.vault("personal", "x.md"))
    assert decision(out) == "deny"
    assert "No vaults are set up for this folder" in reason(out)


def test_session_start_context(world):
    ctx = Session(world, world.site).last["hookSpecificOutput"]["additionalContext"]
    assert ctx == ('vaultlines: save notes from this folder to the `acme-everyone` vault (Basic Memory '
                   'project="acme-everyone", folder ~/Vaults/acme-everyone). You can also read: acme-slack. '
                   'Writing to those asks first. Other vaults are blocked here. '
                   'Always pass project="..." to Basic Memory tools.')


def test_session_start_context_without_plugins(world):
    world.runtime["plugins"] = {}
    ctx = Session(world, world.site).last["hookSpecificOutput"]["additionalContext"]
    assert ctx == ('vaultlines: save notes from this folder to the `acme-everyone` vault '
                   '(folder ~/Vaults/acme-everyone). You can also read: acme-slack. '
                   'Writing to those asks first. Other vaults are blocked here.')


# ---------------------------------------------------------------- paths

def test_paths_relative_home_and_symlinks(world, tmp_path):
    rt = world.runtime
    assert vault_of(world.vault("acme-everyone", "a.md"), rt) == "acme-everyone"
    link = tmp_path / "link"
    os.symlink(world.vault("acme-founders"), link)
    s = Session(world, world.blog)
    for path in (str(link / "x.md"), "~/Vaults/acme-founders/x.md", "../../../Vaults/acme-founders/x.md"):
        assert decision(s.call("Read", file_path=path)) == "deny", path


def test_prefix_of_another_vault_name_is_a_different_vault(world):
    rt = world.runtime
    assert vault_of(world.vault("acme-everyone", "a.md"), rt) == "acme-everyone"
    assert vault_of(world.vault("acme", "a.md"), rt) == "acme"
    assert vault_of(str(world.vaults / "acme-everyoneity" / "a.md"), rt) is None


def test_files_outside_vaults_are_ignored(world):
    s = Session(world, world.site)
    assert s.call("Write", file_path=str(world.site / "README.md")) is None
    assert s.state["read"] == []


def test_grep_over_several_vaults_is_blocked_unless_all_are_in_focus(world):
    s = Session(world, world.site)
    out = s.call("Grep", pattern="x", path=str(world.vaults))
    assert decision(out) == "deny"
    assert "search a narrower folder" in reason(out)
    out = s.call("Glob", pattern=str(world.vaults / "acme-slack" / "**" / "*.md"))
    assert decision(out) is None
    assert s.state["read"] == ["acme-slack"]


def test_grep_without_a_path_searches_the_cwd(world):
    s = Session(world, world.home)
    out = s.call("Grep", pattern="x")  # cwd is home, which holds every vault
    assert decision(out) == "deny"


def test_glob_with_an_absolute_pattern(world):
    s = Session(world, world.site)
    out = s.call("Glob", pattern="~/Vaults/acme-founders/**/*.md")
    assert decision(out) == "deny"
    home = Session(world, world.home)  # cwd holds every vault, but the pattern names one
    assert decision(home.call("Glob", pattern="~/Vaults/personal/**/*.md")) is None
    assert home.state["read"] == ["personal"]


# ---------------------------------------------------------------- Bash

def test_bash_scan(world):
    rt = world.runtime
    assert bash_vaults(f"cat {world.vault('acme-everyone', 'a.md')}", "/", rt) == ["acme-everyone"]
    assert bash_vaults("grep -r x ~/Vaults/acme-founders/", "/", rt) == ["acme-founders"]
    assert bash_vaults('ls "$HOME/Vaults/acme"', "/", rt) == ["acme"]
    assert bash_vaults("ls ${HOME}/Vaults/side/", "/", rt) == ["side"]
    assert bash_vaults("ls Vaults/personal", str(world.home), rt) == ["personal"]
    assert bash_vaults("ls ~/Vaults/acme-everyoneity", "/", rt) == []
    assert bash_vaults("ls", world.vault("docs", "sub"), rt) == ["docs"]  # cwd inside a vault
    assert bash_vaults("echo acme-everyone", "/", rt) == []


def test_bash_counts_as_read_and_write(world):
    s = Session(world, world.acme)
    out = s.call("Bash", command=f"cp ~/Vaults/acme-founders/plan.md {world.vault('acme-everyone')}/")
    assert decision(out) == "ask"
    assert "This session read acme-founders" in reason(out)
    s2 = Session(world, world.site)
    out = s2.call("Bash", command="cat ~/Vaults/acme-slack/x.md")
    assert decision(out) == "ask"
    assert "use Read, Grep or Glob to only read" in reason(out)


def test_bash_in_focus_on_the_writes_vault_is_allowed(world):
    s = Session(world, world.site)
    assert decision(s.call("Bash", command="git -C ~/Vaults/acme-everyone status")) is None


# ---------------------------------------------------------------- Basic Memory through the hook

def test_bm_missing_project_is_filled_in(world):
    s = Session(world, world.acme)
    out = s.bm("search_notes", query="pricing")
    assert out["hookSpecificOutput"]["updatedInput"] == {"query": "pricing", "project": "acme-founders"}
    assert s.state["read"] == ["acme-founders"]


def test_bm_focus_and_label(world):
    s = Session(world, world.acme)
    assert decision(s.bm("read_note", identifier="x", project="personal")) == "deny"
    s.bm("read_note", identifier="plan", project="acme-founders")
    assert decision(s.bm("write_note", title="t", content="c", directory="d", project="acme-everyone")) == "ask"


def test_bm_memory_url_counts_its_project(world):
    s = Session(world, world.site)
    out = s.bm("build_context", url="memory://acme-founders/plan", project="acme-everyone")
    assert decision(out) == "deny"
    assert "`acme-founders` is not used" in reason(out)


def test_bm_project_that_isnt_a_vault(world):
    s = Session(world, world.acme)
    for project in ("main", "nope"):
        out = s.bm("read_note", identifier="x", project=project)
        assert decision(out) == "deny"
        assert "isn't a vault vl knows" in reason(out)


def test_bm_blocked_calls(world):
    s = Session(world, world.acme)
    assert "project_id" in reason(s.bm("read_note", identifier="x", project_id="abc"))
    assert "Searching every project" in reason(s.bm("search_notes", query="x", search_all_projects=True))
    assert "every vault" in reason(s.bm("recent_activity"))
    assert decision(s.bm("recent_activity", project="acme-founders")) is None
    assert decision(s.bm("fetch", id="x")) == "deny"
    assert decision(s.bm("delete_project", project_name="acme-founders")) == "deny"
    assert decision(s.bm("list_memory_projects")) is None


def test_bm_without_a_folder(world, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    s = Session(world, elsewhere)
    assert "No vaults are set up" in reason(s.bm("search_notes", query="x"))


def test_bm_plugin_off_lets_calls_through(world):
    world.runtime["plugins"] = {}
    s = Session(world, world.blog)
    assert s.bm("read_note", identifier="x", project="acme-founders") is None


def test_runtime_from_another_version_blocks_basic_memory(world):
    from conftest import write_runtime

    from vaultlines.hook import run

    world.runtime["version"] = 1
    write_runtime(world)
    event = {"hook_event_name": "PreToolUse", "session_id": "v1", "cwd": str(world.acme),
             "tool_name": "mcp__basic-memory__read_note", "tool_input": {"identifier": "x", "project": "acme-founders"}}
    out = run(event, env={})
    assert decision(out) == "deny"
    assert "vl apply" in reason(out)
    # Calls that can't touch a vault still go through.
    assert run({**event, "cwd": str(world.blog), "tool_name": "Read", "tool_input": {"file_path": "x.md"}}, env={}) is None


# ---------------------------------------------------------------- the plugin's briefing

def _settings(folder, block):
    import json

    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "settings.local.json").write_text(json.dumps({"basicMemory": block}))


def test_plugin_projects_follow_the_nearest_settings_file(world):
    import json

    _settings(world.home, {"primaryProject": "personal"})
    (world.home / ".claude" / "settings.json").write_text(json.dumps({"basicMemory": {"primaryProject": "personal"}}))
    _settings(world.acme, {"primaryProject": "acme-founders", "secondaryProjects": ["acme-slack"]})
    assert plugin_projects(str(world.blog)) == ["personal"]
    assert plugin_projects(str(world.api)) == ["acme-founders", "acme-slack"]
    # A repo with its own settings file but no block: the plugin falls back to the user-level block.
    (world.api / ".claude").mkdir()
    (world.api / ".claude" / "settings.json").write_text("{}")
    assert plugin_projects(str(world.api)) == ["personal"]
    # A broken settings file turns the plugin off.
    (world.api / ".claude" / "settings.json").write_text("{nope")
    assert plugin_projects(str(world.api)) == []


# ---------------------------------------------------------------- vl's own files and commands

def _vl_world(world, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(world.home / ".config"))
    world.runtime["config"] = str(world.home / ".config" / "vaultlines" / "config.toml")
    return world.home / ".local" / "state" / "vaultlines", world.home / ".config" / "vaultlines"


def test_vl_records_can_never_be_written(world, monkeypatch):
    state, _ = _vl_world(world, monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(world.home / ".local" / "state"))
    s = Session(world, world.site)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "please fix vl for me"})
    for path in (state / "runtime.json", state / "sessions" / "s1.json"):
        out = s.call("Write", file_path=str(path), content="{}")
        assert decision(out) == "deny"
        assert "can only be changed by vl" in reason(out)
    assert decision(s.call("Read", file_path=str(state / "runtime.json"))) is None


def test_vl_commands_and_config_ask_unless_you_asked(world, monkeypatch):
    _, config = _vl_world(world, monkeypatch)
    s = Session(world, world.site)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "update the notes"})
    for tool, args in (("Bash", {"command": "vl folder set . --reads acme-founders"}),
                       ("Bash", {"command": "cd /tmp && ~/.local/bin/vl uninstall"}),
                       ("Bash", {"command": "cat ~/.config/vaultlines/config.toml"}),
                       ("Bash", {"command": "echo '{\"disableAllHooks\": true}' > .claude/settings.json"}),
                       ("Edit", {"file_path": str(config / "config.toml"), "old_string": "a", "new_string": "b"})):
        assert decision(s.call(tool, **args)) == "ask", args
    assert "Mention vl in your message" in reason(s.call("Bash", command="vl apply"))
    for harmless in ("vl status", "vl doctor", "vl sync", "vl check", "echo evaluate this"):
        assert s.call("Bash", command=harmless) is None, harmless

    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "Run VL apply please"})
    assert s.call("Bash", command="vl folder set . --reads acme-founders") is None
    assert s.call("Edit", file_path=str(config / "config.toml"), old_string="a", new_string="b") is None
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "thanks, now the README"})
    assert decision(s.call("Bash", command="vl apply")) == "ask"


def test_you_asked_doesnt_skip_leak_checks(world, monkeypatch):
    _vl_world(world, monkeypatch)
    s = Session(world, world.acme)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "use vl to copy the plan"})
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Bash", command=f"cp ~/Vaults/acme-founders/plan.md {world.vault('acme-everyone')}/ && vl sync")
    assert decision(out) == "ask"
    assert "ana would see this" in reason(out)


def test_guard_and_leak_reasons_combine(world, monkeypatch):
    _vl_world(world, monkeypatch)
    s = Session(world, world.acme)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Bash", command="cp ~/Vaults/acme-founders/p.md ~/Vaults/acme-everyone/ && vl apply")
    assert decision(out) == "ask"
    assert "vl apply" in reason(out) and "ana would see this" in reason(out)


def test_settings_edits_that_switch_vl_off_ask(world, monkeypatch):
    import json

    _vl_world(world, monkeypatch)
    settings = world.home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    ours = {"hooks": {"PreToolUse": [{"matcher": "x", "hooks": [{"type": "command", "command": "/u/.local/bin/vl hook"}]}]}}
    settings.write_text(json.dumps(ours))
    s = Session(world, world.blog)
    assert decision(s.call("Write", file_path=str(settings), content=json.dumps({"model": "opus"}))) == "ask"
    assert decision(s.call("Write", file_path=str(settings), content=json.dumps({**ours, "disableAllHooks": True}))) == "ask"
    assert s.call("Write", file_path=str(settings), content=json.dumps({**ours, "model": "opus"})) is None
    assert decision(s.call("Edit", file_path=str(settings), old_string='"command": "/u/.local/bin/vl hook"',
                           new_string='"command": "true"')) == "ask"
    assert decision(s.call("MultiEdit", file_path=str(world.blog / ".claude" / "settings.local.json"),
                           edits=[{"old_string": "{", "new_string": '{"disableAllHooks": true,'}])) == "ask"
    assert s.call("Edit", file_path=str(settings), old_string="opus", new_string="sonnet") is None


def test_prompt_before_any_session_record(world):
    out, state = decide({"hook_event_name": "UserPromptSubmit", "session_id": "new", "cwd": str(world.site),
                         "prompt": "vl status?"}, world.runtime, None, str(world.site))
    assert out is None
    assert state["asked_vl"] is True and state["label"] == []


# ---------------------------------------------------------------- a Drive source's vault, fetch and rclone

def _drive_world(world):
    """acme-drive: filled from Drive on this computer, read in the acme folder."""
    fetch = world.home / ".cache" / "vaultlines" / "fetch" / "acme-drive"
    (world.vaults / "acme-drive").mkdir()
    world.runtime["vaults"]["acme-drive"] = {
        "paths": [world.vault("acme-drive")], "show": "~/Vaults/acme-drive",
        "audience": {"kind": "people", "logins": ["sam", "Lee"], "reason": ""},
        "source": "drive", "fetch": [str(fetch)]}
    world.runtime["folders"][str(world.acme)]["reads"].append("acme-drive")
    world.runtime["plugins"]["acme-drive"] = {
        "kind": "drive", "tool_prefixes": [],
        "data": {"vault": "acme-drive", "remote": "vl-acme-drive", "rclone_config": str(world.home / ".config" / "rclone" / "rclone.conf")}}
    return fetch


def test_vl_fetch_is_a_read_of_its_vault(world):
    _drive_world(world)
    s = Session(world, world.acme)
    assert s.call("Bash", command='vl fetch acme-drive "Finance/Runway.xlsx"') is None
    assert s.state["read"] == ["acme-drive"]
    out = s.call("Write", file_path=world.vault("acme-everyone", "x.md"))
    assert decision(out) == "ask" and "ana would see this in acme-everyone" in reason(out)


def test_vl_fetch_of_a_vault_the_folder_doesnt_use_is_denied(world):
    _drive_world(world)
    s = Session(world, world.site)
    out = s.call("Bash", command="cd /tmp && ~/.local/bin/vl fetch 'acme-drive' x.pdf")
    assert decision(out) == "deny"
    assert "`acme-drive` is not used in this folder" in reason(out)


def test_the_fetch_folder_counts_as_its_vault(world):
    fetch = _drive_world(world)
    s = Session(world, world.acme)
    assert s.call("Read", file_path=str(fetch / "Finance" / "Runway.xlsx")) is None
    assert s.state["read"] == ["acme-drive"] and s.label == frozenset({"sam", "lee"})
    assert s.call("Grep", pattern="cash", path=str(fetch)) is None
    assert s.call("Bash", command="python3 -c 'import openpyxl' ~/.cache/vaultlines/fetch/acme-drive/Finance/Runway.xlsx") is None
    for tool in ("Write", "Edit"):
        out = s.call(tool, file_path=str(fetch / "Finance" / "Runway.xlsx"), content="x")
        assert decision(out) == "deny" and "fetched originals are read-only copies" in reason(out)


def test_the_fetch_folder_is_denied_where_its_vault_isnt_used(world):
    fetch = _drive_world(world)
    s = Session(world, world.site)
    assert decision(s.call("Read", file_path=str(fetch / "a.pdf"))) == "deny"
    assert decision(s.call("Bash", command=f"cat {fetch}/a.pdf")) == "deny"


def test_rclone_on_the_source_remote_is_denied(world):
    _drive_world(world)
    s = Session(world, world.acme)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "use vl to get the file"})
    for command in ("rclone lsf vl-acme-drive:", "rclone copy 'vl-acme-drive,team_drive=0AB:x' /tmp",
                    "/opt/homebrew/bin/rclone cat vl-acme-drive:Finance/Runway.xlsx", "rclone config dump",
                    "cd /tmp; rclone config show vl-acme-drive"):
        out = s.call("Bash", command=command)
        assert decision(out) == "deny", command
        assert "vl fetch" in reason(out)
    assert s.call("Bash", command="rclone lsf other:") is None
    assert s.call("Bash", command="echo vl-acme-drive") is None


def test_the_rclone_config_is_off_limits(world):
    _drive_world(world)
    conf = world.home / ".config" / "rclone" / "rclone.conf"
    s = Session(world, world.blog)
    for tool, args in (("Read", {"file_path": str(conf)}), ("Edit", {"file_path": str(conf)}),
                       ("Write", {"file_path": str(conf), "content": ""}),
                       ("Bash", {"command": "cat ~/.config/rclone/rclone.conf"}),
                       ("Bash", {"command": f"grep token {conf}"})):
        out = s.call(tool, **args)
        assert decision(out) == "deny", (tool, args)
        assert "rclone config" in reason(out)


def test_the_briefing_says_how_to_fetch(world):
    _drive_world(world)
    ctx = Session(world, world.acme).last["hookSpecificOutput"]["additionalContext"]
    assert ctx.endswith('`acme-drive` holds notes converted from Google Drive by vl. For an original, run '
                        '`vl fetch acme-drive "<path from the note\'s frontmatter>"`.')
    assert "vl fetch" not in Session(world, world.site).last["hookSpecificOutput"]["additionalContext"]
