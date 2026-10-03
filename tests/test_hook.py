"""decide(): the hook's rules, one scenario at a time."""

from __future__ import annotations

import json
import os

import pytest

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

    @property
    def rules(self):
        return self.state["rules"]

    @property
    def context(self):
        return self.last["hookSpecificOutput"]["additionalContext"]


def decision(out):
    if out is None:
        return None
    return out["hookSpecificOutput"].get("permissionDecision")


def reason(out):
    return out["hookSpecificOutput"].get("permissionDecisionReason", "")


ACME = ["acme-docs", "acme-drive", "acme-everyone", "acme-founders", "acme-handbook", "acme-sam-personal", "acme-slack"]


def others(writes):
    return [v for v in ACME if v != writes]


# ---------------------------------------------------------------- which vaults a session uses

@pytest.mark.parametrize("where, writes, reads", [
    ("site", "acme-everyone", others("acme-everyone")),      # in notes_from
    ("api", "acme-everyone", others("acme-everyone")),       # a folder inside that repo
    ("app", "acme-everyone", others("acme-everyone")),       # cloned anywhere
    ("legal", "acme-founders", others("acme-founders")),
    ("sheety", "acme-sam-personal", others("acme-sam-personal")),  # in no notes_from
    ("both", "acme-sam-personal", others("acme-sam-personal")),    # in two notes_from
    ("blog", "sam-personal", ["sam-recipes", "sam-side"]),   # your own account
    ("oss", "sam-personal", []),                             # an owner you didn't join
    ("desktop", "sam-personal", []),                         # no repo
    ("writing", "sam-recipes", []),                          # a [folders] entry
])
def test_where_claude_starts_decides_the_vaults(world, where, writes, reads):
    s = Session(world, getattr(world, where))
    assert (s.rules["writes"], s.rules["reads"]) == (writes, reads)


def test_focus_blocks_another_owners_vault(world):
    s = Session(world, world.site)
    out = s.call("Read", file_path=world.vault("sam-side", "idea.md"))
    assert decision(out) == "deny"
    assert "`sam-side` isn't used here" in reason(out)
    assert "This session uses acme-everyone, acme-docs" in reason(out)


def test_focus_blocks_org_vaults_outside_org_repos(world):
    s = Session(world, world.desktop)
    assert decision(s.call("Read", file_path=world.vault("acme-founders", "plan.md"))) == "deny"
    assert decision(s.call("Read", file_path=world.vault("sam-personal", "x.md"))) is None


def test_a_config_entry_can_add_another_owners_vault(world):
    world.runtime["repos"]["acme/*"] = {"writes": None, "reads": ["sam-side"]}
    s = Session(world, world.site)
    assert decision(s.call("Read", file_path=world.vault("sam-side", "idea.md"))) is None


def test_rules_come_from_the_repo_not_the_folder_it_is_in(world):
    # The site repo is inside ~/code, which a [folders] entry covers; the repo wins.
    world.runtime["folders"][str(world.home / "code")] = {"writes": "sam-recipes", "reads": []}
    assert Session(world, world.site).rules["writes"] == "acme-everyone"
    assert Session(world, world.home / "code").rules["writes"] == "sam-recipes"


# ---------------------------------------------------------------- the label

def test_same_audience_read_then_write_is_allowed(world):
    s = Session(world, world.site)
    assert decision(s.call("Read", file_path=world.vault("acme-slack", "general.md"))) is None
    assert decision(s.call("Write", file_path=world.vault("acme-everyone", "note.md"), content="x")) is None


def test_narrower_read_then_write_asks_and_names_the_new_people(world):
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Write", file_path=world.vault("acme-everyone", "note.md"), content="x")
    assert decision(out) == "ask"
    assert "This session read acme-founders." in reason(out)
    assert "ana would see this in acme-everyone" in reason(out)


def test_wider_read_then_narrower_write_is_allowed(world):
    s = Session(world, world.legal)
    s.call("Read", file_path=world.vault("acme-everyone", "a.md"))
    assert decision(s.call("Edit", file_path=world.vault("acme-founders", "b.md"))) is None


def test_reading_a_vault_only_you_see_makes_shared_writes_ask(world):
    s = Session(world, world.legal)
    s.call("Read", file_path=world.vault("acme-sam-personal", "idea.md"))
    assert s.label == lbl.ONLY_YOU
    out = s.call("Write", file_path=world.vault("acme-founders", "x.md"))
    assert decision(out) == "ask"
    assert "Lee would see this" in reason(out)


def test_other_tools_dont_change_the_label(world):
    s = Session(world, world.site)
    before = dict(s.state)
    assert s.call("WebFetch", url="https://example.com") is None
    assert s.call("mcp__claude_ai_Gmail__get_thread", id="1") is None
    assert s.state == before
    assert decision(s.call("Write", file_path=world.vault("acme-everyone", "n.md"))) is None


def test_a_new_person_on_github_makes_a_safe_write_ask(world):
    world.runtime["vaults"]["acme-everyone"]["audience"]["logins"].append("contractor")
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-slack", "general.md"))
    out = s.call("Write", file_path=world.vault("acme-everyone", "note.md"))
    assert decision(out) == "ask"
    assert "contractor would see this" in reason(out)


def test_on_leak_block_denies(world):
    world.runtime["on_leak"] = "block"
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Write", file_path=world.vault("acme-everyone", "note.md"))
    assert decision(out) == "deny"
    assert 'on_leak = "block"' in reason(out)


def test_writing_a_reads_vault_always_asks(world):
    s = Session(world, world.site)
    out = s.call("Write", file_path=world.vault("acme-slack", "x.md"))
    assert decision(out) == "ask"
    assert "`acme-slack` is a read vault here" in reason(out)


def test_unknown_audience_read_counts_as_only_you(world):
    s = Session(world, world.sheety)
    s.call("Grep", pattern="x", path=world.vault("acme-handbook"))
    assert s.label == lbl.ONLY_YOU
    assert s.state["read"] == ["acme-handbook"]


def test_public_read_changes_nothing(world):
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-docs", "readme.md"))
    assert s.label is lbl.EVERYONE
    assert s.state["read"] == ["acme-docs"]


def test_writing_an_unknown_audience_vault_after_a_restricted_read_asks(world):
    world.runtime["repos"]["acme/sheety"] = {"writes": "acme-handbook", "reads": []}
    s = Session(world, world.sheety)
    s.call("Read", file_path=world.vault("acme-founders", "x.md"))
    out = s.call("Write", file_path=world.vault("acme-handbook", "x.md"))
    assert decision(out) == "ask"
    assert "people vl can't list" in reason(out)


def test_only_you_vault_takes_any_write(world):
    s = Session(world, world.sheety)
    s.call("Read", file_path=world.vault("acme-founders", "x.md"))
    assert decision(s.call("Write", file_path=world.vault("acme-sam-personal", "x.md"))) is None


def test_denied_calls_dont_count_as_reads(world):
    s = Session(world, world.blog)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    assert s.state["read"] == []
    assert s.label is lbl.EVERYONE


def test_asked_calls_count_as_reads(world):
    s = Session(world, world.site)
    s.call("Bash", command=f"cat {world.vault('acme-slack', 'x.md')}")
    assert decision(s.last) == "ask"
    assert s.state["read"] == ["acme-slack"]


def test_login_case_doesnt_matter(world):
    world.runtime["me"] = "SAM"
    world.runtime["vaults"]["acme-founders"]["audience"]["logins"] = ["Sam", "lee"]
    s = Session(world, world.legal)
    s.call("Read", file_path=world.vault("acme-everyone", "a.md"))
    assert decision(s.call("Write", file_path=world.vault("acme-founders", "b.md"))) is None


# ---------------------------------------------------------------- vaults with a source

def test_a_drive_vault_is_read_only(world):
    s = Session(world, world.site)
    assert decision(s.call("Read", file_path=world.vault("acme-drive", "Finance", "Runway.xlsx.md"))) is None
    for tool in ("Write", "Edit"):
        out = s.call(tool, file_path=world.vault("acme-drive", "x.md"), content="x")
        assert decision(out) == "deny"
        assert "`acme-drive` comes from Google Drive" in reason(out)
    out = s.bm("write_note", title="t", content="c", directory="d", project="acme-drive")
    assert decision(out) == "deny"


def test_a_drive_vault_is_read_only_even_where_config_says_writes(world):
    world.runtime["repos"]["acme/site"] = {"writes": "acme-drive", "reads": []}  # vl apply leaves this out
    s = Session(world, world.site)
    assert decision(s.call("Write", file_path=world.vault("acme-drive", "x.md"))) == "deny"


def test_bash_on_a_drive_vault_only_reads(world):
    s = Session(world, world.site)
    assert s.call("Bash", command=f"grep -r cash {world.vault('acme-drive')}") is None
    assert s.state["read"] == ["acme-drive"]


def test_vl_source_fetch_is_a_read_of_its_vault(world):
    s = Session(world, world.site)
    assert s.call("Bash", command='vl source fetch acme/vault-drive "Finance/Runway.xlsx"') is None
    assert s.state["read"] == ["acme-drive"]
    out = s.call("Write", file_path=world.vault("acme-everyone", "x.md"))
    assert decision(out) == "ask" and "ana would see this in acme-everyone" in reason(out)


def test_vl_source_fetch_by_short_name_too(world):
    s = Session(world, world.site)
    assert s.call("Bash", command="vl source fetch acme-drive x.pdf") is None
    assert s.state["read"] == ["acme-drive"]


def test_vl_source_fetch_of_a_vault_not_used_here_is_denied(world):
    s = Session(world, world.blog)
    out = s.call("Bash", command="cd /tmp && ~/.local/bin/vl source fetch 'acme/vault-drive' x.pdf")
    assert decision(out) == "deny"
    assert "`acme-drive` isn't used here" in reason(out)


def test_the_fetch_folder_counts_as_its_vault(world):
    s = Session(world, world.site)
    assert s.call("Read", file_path=str(world.fetch / "Finance" / "Runway.xlsx")) is None
    assert s.state["read"] == ["acme-drive"] and s.label == frozenset({"sam", "lee"})
    assert s.call("Grep", pattern="cash", path=str(world.fetch)) is None
    assert s.call("Bash", command="python3 -c 'import openpyxl' ~/.vaultlines/cache/fetch/acme/vault-drive/Finance/Runway.xlsx") is None
    for tool in ("Write", "Edit"):
        out = s.call(tool, file_path=str(world.fetch / "Finance" / "Runway.xlsx"), content="x")
        assert decision(out) == "deny" and "fetched originals are read-only copies" in reason(out)


def test_the_fetch_folder_is_denied_where_its_vault_isnt_used(world):
    s = Session(world, world.blog)
    assert decision(s.call("Read", file_path=str(world.fetch / "a.pdf"))) == "deny"
    assert decision(s.call("Bash", command=f"cat {world.fetch}/a.pdf")) == "deny"


# ---------------------------------------------------------------- the briefing

def test_the_briefing(world):
    ctx = Session(world, world.site).context
    assert ctx.startswith('vaultlines: save notes from this repo to `acme-everyone` (Basic Memory '
                          'project="acme-everyone"): Notes everyone at Acme can see. You can also read: '
                          '`acme-docs`, `acme-drive` (The text of every file in the Acme shared drive. Claude only…), ')
    assert "`acme-founders` (Founders' notes: fundraising, hiring.)" in ctx
    assert "Writing to those asks first. Other vaults are blocked here. " in ctx
    assert 'Always pass project="..." to Basic Memory tools.' in ctx
    assert ctx.endswith('`acme-drive` holds notes converted from Google Drive; for an original, run '
                        '`vl source fetch acme/vault-drive "<path from the note\'s frontmatter>"`.')


def test_the_briefing_without_plugins_or_reads(world):
    world.runtime["plugins"] = {}
    ctx = Session(world, world.desktop).context
    assert ctx == ("vaultlines: save notes from this folder to `sam-personal`: sam's personal notes. "
                   "Other vaults are blocked here.")


def test_the_briefing_says_why_on_a_conflict(world):
    ctx = Session(world, world.both).context
    assert ("acme/both is in notes_from of two vaults (acme-everyone, acme-founders), so its notes go to your "
            "personal vault `acme-sam-personal`.") in ctx


def test_the_briefing_without_a_writes_vault(world):
    world.runtime["default"] = {"writes": None, "reads": []}
    s = Session(world, world.desktop)
    assert s.last is None
    out = s.call("Read", file_path=world.vault("sam-personal", "x.md"))
    assert decision(out) == "deny" and "No vaults are set up here" in reason(out)


# ---------------------------------------------------------------- sessions

def test_briefing_counts_as_a_read(world):
    s = Session(world, world.site, briefing=["acme-founders"])
    assert s.state["read"] == ["acme-founders"]
    assert decision(s.call("Write", file_path=world.vault("acme-everyone", "n.md"))) == "ask"


def test_fork_starts_as_only_you(world):
    s = Session(world, world.site, source="fork")
    assert s.label == lbl.ONLY_YOU
    out = s.call("Write", file_path=world.vault("acme-everyone", "n.md"))
    assert decision(out) == "ask"
    assert "because it was forked" in reason(out)


def test_resume_keeps_the_label(world):
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    resumed = Session(world, world.site, source="resume", state=s.state)
    assert resumed.state["read"] == ["acme-founders"]
    assert resumed.label == s.label


def test_resume_elsewhere_keeps_the_original_rules(world):
    s = Session(world, world.site)
    resumed = Session(world, world.blog, source="resume", state=s.state)
    assert resumed.rules["writes"] == "acme-everyone"
    assert "`acme-everyone`" in resumed.context


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
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    cleared = Session(world, world.site, source="clear", state=s.state)
    assert cleared.label is lbl.EVERYONE
    assert cleared.state["read"] == []


def test_the_rules_are_where_the_session_started(world):
    s = Session(world, world.site)
    s.folder = str(world.blog)  # Claude cd'd somewhere else; the tool call's cwd changes
    assert decision(s.call("Read", file_path=world.vault("acme-slack", "x.md"))) is None


# ---------------------------------------------------------------- paths

def test_paths_relative_home_and_symlinks(world, tmp_path):
    rt = world.runtime
    assert vault_of(world.vault("acme-everyone", "a.md"), rt) == "acme-everyone"
    link = tmp_path / "link"
    os.symlink(world.vault("acme-founders"), link)
    s = Session(world, world.blog)
    for path in (str(link / "x.md"), "~/.vaultlines/vaults/acme/vault-founders/x.md",
                 "../../.vaultlines/vaults/acme/vault-founders/x.md"):
        assert decision(s.call("Read", file_path=path)) == "deny", path


def test_a_prefix_of_another_vaults_folder_is_a_different_folder(world):
    rt = world.runtime
    assert vault_of(world.vault("acme-everyone", "a.md"), rt) == "acme-everyone"
    assert vault_of(str(world.vaults / "acme" / "vault-everyoneity" / "a.md"), rt) is None


def test_files_outside_vaults_are_ignored(world):
    s = Session(world, world.site)
    assert s.call("Write", file_path=str(world.site / "README.md")) is None
    assert s.state["read"] == []


def test_grep_over_several_vaults_is_blocked_unless_all_are_in_focus(world):
    s = Session(world, world.blog)
    out = s.call("Grep", pattern="x", path=str(world.vaults))
    assert decision(out) == "deny"
    assert "search a narrower folder" in reason(out)
    out = s.call("Glob", pattern=str(world.vaults / "sam" / "vault-side" / "**" / "*.md"))
    assert decision(out) is None
    assert s.state["read"] == ["sam-side"]


def test_grep_over_all_of_an_owners_vaults_is_fine_where_all_are_used(world):
    s = Session(world, world.site)
    assert decision(s.call("Grep", pattern="x", path=str(world.vaults / "acme"))) is None


def test_grep_without_a_path_searches_the_cwd(world):
    s = Session(world, world.home)
    out = s.call("Grep", pattern="x")  # cwd is home, which holds every vault
    assert decision(out) == "deny"


def test_glob_with_an_absolute_pattern(world):
    s = Session(world, world.blog)
    out = s.call("Glob", pattern="~/.vaultlines/vaults/acme/vault-founders/**/*.md")
    assert decision(out) == "deny"
    assert decision(s.call("Glob", pattern="~/.vaultlines/vaults/sam/vault-side/**/*.md")) is None
    assert s.state["read"] == ["sam-side"]


# ---------------------------------------------------------------- Bash

def test_bash_scan(world):
    rt = world.runtime
    assert bash_vaults(f"cat {world.vault('acme-everyone', 'a.md')}", "/", rt) == ["acme-everyone"]
    assert bash_vaults("grep -r x ~/.vaultlines/vaults/acme/vault-founders/", "/", rt) == ["acme-founders"]
    assert bash_vaults('ls "$HOME/.vaultlines/vaults/sam/vault-side"', "/", rt) == ["sam-side"]
    assert bash_vaults("ls ${HOME}/.vaultlines/vaults/sam/vault-recipes/", "/", rt) == ["sam-recipes"]
    assert bash_vaults("ls vault-docs", str(world.vaults / "acme"), rt) == ["acme-docs"]
    assert bash_vaults("ls ~/.vaultlines/vaults/acme/vault-everyoneity", "/", rt) == []
    assert bash_vaults("ls", world.vault("acme-docs", "sub"), rt) == ["acme-docs"]  # cwd inside a vault
    assert bash_vaults("echo acme-everyone", "/", rt) == []


def test_bash_counts_as_read_and_write(world):
    s = Session(world, world.site)
    out = s.call("Bash", command=f"cp ~/.vaultlines/vaults/acme/vault-founders/plan.md {world.vault('acme-everyone')}/")
    assert decision(out) == "ask"
    assert "This session read acme-founders" in reason(out)
    s2 = Session(world, world.site)
    out = s2.call("Bash", command="cat ~/.vaultlines/vaults/acme/vault-slack/x.md")
    assert decision(out) == "ask"
    assert "use Read, Grep or Glob to only read" in reason(out)


def test_bash_on_the_writes_vault_is_allowed(world):
    s = Session(world, world.site)
    assert decision(s.call("Bash", command="git -C ~/.vaultlines/vaults/acme/vault-everyone status")) is None


# ---------------------------------------------------------------- Basic Memory through the hook

def test_bm_missing_project_is_filled_in(world):
    s = Session(world, world.legal)
    out = s.bm("search_notes", query="pricing")
    assert out["hookSpecificOutput"]["updatedInput"] == {"query": "pricing", "project": "acme-founders"}
    assert s.state["read"] == ["acme-founders"]


def test_bm_focus_and_label(world):
    s = Session(world, world.site)
    assert decision(s.bm("read_note", identifier="x", project="sam-personal")) == "deny"
    s.bm("read_note", identifier="plan", project="acme-founders")
    assert decision(s.bm("write_note", title="t", content="c", directory="d", project="acme-everyone")) == "ask"


def test_bm_memory_url_counts_its_project(world):
    s = Session(world, world.blog)
    out = s.bm("build_context", url="memory://acme-founders/plan", project="sam-personal")
    assert decision(out) == "deny"
    assert "`acme-founders` isn't used" in reason(out)


def test_bm_project_that_isnt_a_vault(world):
    s = Session(world, world.site)
    for project in ("main", "nope"):
        out = s.bm("read_note", identifier="x", project=project)
        assert decision(out) == "deny"
        assert "isn't a vault vl knows" in reason(out)


def test_bm_blocked_calls(world):
    s = Session(world, world.site)
    assert "project_id" in reason(s.bm("read_note", identifier="x", project_id="abc"))
    assert "Searching every project" in reason(s.bm("search_notes", query="x", search_all_projects=True))
    assert "every vault" in reason(s.bm("recent_activity"))
    assert decision(s.bm("recent_activity", project="acme-founders")) is None
    assert decision(s.bm("fetch", id="x")) == "deny"
    assert decision(s.bm("delete_project", project_name="acme-founders")) == "deny"
    assert decision(s.bm("list_memory_projects")) is None


def test_bm_plugin_off_lets_calls_through(world):
    world.runtime["plugins"] = {}
    s = Session(world, world.blog)
    assert s.bm("read_note", identifier="x", project="acme-founders") is None


def test_runtime_from_another_version_blocks_basic_memory(world):
    from conftest import write_runtime

    from vaultlines.hook import run

    world.runtime["version"] = 3
    write_runtime(world)
    event = {"hook_event_name": "PreToolUse", "session_id": "v1", "cwd": str(world.site),
             "tool_name": "mcp__basic-memory__read_note", "tool_input": {"identifier": "x", "project": "acme-founders"}}
    out = run(event, env={})
    assert decision(out) == "deny"
    assert "vl apply" in reason(out)
    # Calls that can't touch a vault still go through.
    assert run({**event, "cwd": str(world.blog), "tool_name": "Read", "tool_input": {"file_path": "x.md"}}, env={}) is None


# ---------------------------------------------------------------- the plugin's briefing

def _settings(folder, block):
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "settings.local.json").write_text(json.dumps({"basicMemory": block}))


def test_plugin_projects_follow_the_nearest_settings_file(world):
    (world.home / ".claude").mkdir()
    (world.home / ".claude" / "settings.json").write_text(json.dumps({"basicMemory": {"primaryProject": "sam-personal"}}))
    _settings(world.site, {"primaryProject": "acme-everyone", "secondaryProjects": ["acme-slack"]})
    assert plugin_projects(str(world.blog)) == ["sam-personal"]
    assert plugin_projects(str(world.api)) == ["acme-everyone", "acme-slack"]
    # A folder with its own settings file but no block: the plugin falls back to the user-level block.
    (world.api / ".claude").mkdir()
    (world.api / ".claude" / "settings.json").write_text("{}")
    assert plugin_projects(str(world.api)) == ["sam-personal"]
    # A broken settings file turns the plugin off.
    (world.api / ".claude" / "settings.json").write_text("{nope")
    assert plugin_projects(str(world.api)) == []


# ---------------------------------------------------------------- vl's own files and commands

def test_vl_records_can_never_be_written(world):
    state = world.vl / "state"
    s = Session(world, world.site)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "please fix vl for me"})
    for path in (state / "runtime.json", state / "sessions" / "s1.json", state / "clones.json"):
        out = s.call("Write", file_path=str(path), content="{}")
        assert decision(out) == "deny"
        assert "can only be changed by vl" in reason(out)
    assert decision(s.call("Read", file_path=str(state / "runtime.json"))) is None


def test_google_logins_are_off_limits(world):
    google = world.vl / "google" / "1234-abc.json"
    s = Session(world, world.site)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "use vl to look at the token"})
    for tool, args in (("Read", {"file_path": str(google)}), ("Write", {"file_path": str(google), "content": ""}),
                       ("Grep", {"pattern": "refresh", "path": str(google.parent)}),
                       ("Bash", {"command": "cat ~/.vaultlines/google/1234-abc.json"}),
                       ("Bash", {"command": f"ls {google.parent}"})):
        out = s.call(tool, **args)
        assert decision(out) == "deny", (tool, args)
        assert "Google logins" in reason(out)


def test_vl_commands_and_config_ask_unless_you_asked(world):
    config = world.vl / "config.toml"
    s = Session(world, world.site)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "update the notes"})
    for tool, args in (("Bash", {"command": "vl org leave acme"}),
                       ("Bash", {"command": "cd /tmp && ~/.local/bin/vl uninstall"}),
                       ("Bash", {"command": "vl vault create acme/vault-x"}),
                       ("Bash", {"command": "vl vault create acme/vault-hq --source gdrive"}),
                       ("Bash", {"command": "vl source login acme/vault-drive"}),
                       ("Bash", {"command": "vl source refresh acme/vault-drive"}),  # pushes a read-only vault
                       ("Bash", {"command": "vl source refresh --convert-only"}),
                       ("Bash", {"command": "cat ~/.vaultlines/config.toml"}),
                       ("Bash", {"command": "VAULTLINES_HOME=/tmp/x vl status"}),
                       ("Bash", {"command": "echo '{\"disableAllHooks\": true}' > .claude/settings.json"}),
                       ("Edit", {"file_path": str(config), "old_string": "a", "new_string": "b"})):
        assert decision(s.call(tool, **args)) == "ask", args
    assert "Mention vl in your message" in reason(s.call("Bash", command="vl apply"))
    for harmless in ("vl status", "vl doctor", "vl sync", "vl check", "vl source fetch acme/vault-drive x",
                     "echo evaluate this"):
        assert decision(s.call("Bash", command=harmless)) is None, harmless

    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "Run VL apply please"})
    assert s.call("Bash", command="vl org join acme") is None
    assert s.call("Edit", file_path=str(config), old_string="a", new_string="b") is None
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "thanks, now the README"})
    assert decision(s.call("Bash", command="vl apply")) == "ask"


def test_vault_files_in_the_vl_home_are_not_vls_own(world):
    s = Session(world, world.site)
    assert s.call("Write", file_path=world.vault("acme-everyone", "x.md"), content="x") is None
    assert s.call("Bash", command="ls ~/.vaultlines/vaults/acme/vault-everyone") is None


def test_you_asked_doesnt_skip_leak_checks(world):
    s = Session(world, world.site)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "use vl to copy the plan"})
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Bash", command=f"cp ~/.vaultlines/vaults/acme/vault-founders/plan.md {world.vault('acme-everyone')}/ && vl sync")
    assert decision(out) == "ask"
    assert "ana would see this" in reason(out)


def test_guard_and_leak_reasons_combine(world):
    s = Session(world, world.site)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    out = s.call("Bash", command="cp ~/.vaultlines/vaults/acme/vault-founders/p.md ~/.vaultlines/vaults/acme/vault-everyone/ && vl apply")
    assert decision(out) == "ask"
    assert "vl apply" in reason(out) and "ana would see this" in reason(out)


def test_settings_edits_that_switch_vl_off_ask(world):
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


def test_dev_mode_skips_every_guard(world):
    """For working on vl itself: dangerously_skip_hook_guards, and the hook allows every call
    in that repo."""
    world.runtime["repos"]["acme/site"] = {"writes": None, "reads": [], "dangerously_skip_hook_guards": True}
    s = Session(world, world.site)
    out = json.dumps(s.event({"hook_event_name": "SessionStart", "source": "startup"}))
    assert "dangerously_skip_hook_guards" in out and "Other vaults are blocked here" not in out
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "tidy up"})
    google = world.vl / "google" / "x.json"
    for tool, args in (("Read", {"file_path": world.vault("sam-side", "idea.md")}),  # another owner's vault
                       ("Read", {"file_path": str(google)}),
                       ("Write", {"file_path": str(world.vl / "state" / "runtime.json"), "content": "{}"}),
                       ("Write", {"file_path": world.vault("acme-drive", "x.md"), "content": "x"}),  # from a source
                       ("Bash", {"command": f"vl apply && cat {google}"}),
                       ("Bash", {"command": "VAULTLINES_HOME=/tmp/x vl status"}),
                       ("Edit", {"file_path": str(world.vl / "config.toml"), "old_string": "a", "new_string": "b"}),
                       ("mcp__basic-memory__read_note", {"identifier": "x", "project": "acme-founders"})):
        assert s.call(tool, **args) is None, (tool, args)
    s.call("Read", file_path=world.vault("acme-founders", "plan.md"))
    assert s.call("Write", file_path=world.vault("acme-everyone", "p.md"), content="x") is None  # no leak check
    # Other repos are guarded as always.
    other = Session(world, world.legal)
    other.event({"hook_event_name": "UserPromptSubmit", "prompt": "tidy up"})
    assert decision(other.call("Read", file_path=str(google))) == "deny"
    assert decision(other.call("Bash", command="vl apply")) == "ask"


def test_dev_mode_takes_effect_in_running_sessions(world):
    s = Session(world, world.blog)
    s.event({"hook_event_name": "UserPromptSubmit", "prompt": "tidy up"})
    assert decision(s.call("Bash", command="vl apply")) == "ask"
    world.runtime["repos"]["sam/*"] = {"writes": None, "reads": [], "dangerously_skip_hook_guards": True}
    assert s.call("Bash", command="vl apply") is None
    assert s.call("Read", file_path=str(world.vl / "google" / "x.json")) is None


