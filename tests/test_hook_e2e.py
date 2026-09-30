"""Run the real `vl hook` command on hook events shaped like the ones Claude Code 2.1.286 sends."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import write_runtime

VL = str(Path(sys.executable).parent / "vl")


def hook(world, event: dict, project_dir=None) -> dict | None:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir or event.get("cwd", ""))}
    result = subprocess.run([VL, "hook"], input=json.dumps(event), capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def start(session_id, cwd, source="startup"):
    return {"session_id": session_id, "transcript_path": f"/tmp/{session_id}.jsonl", "cwd": str(cwd),
            "hook_event_name": "SessionStart", "source": source}


def tool(session_id, cwd, name, tool_input, **extra):
    return {"session_id": session_id, "transcript_path": f"/tmp/{session_id}.jsonl", "cwd": str(cwd),
            "prompt_id": "p1", "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse",
            "tool_name": name, "tool_input": tool_input, "tool_use_id": "toolu_1", **extra}


def decision(out):
    return out and out["hookSpecificOutput"].get("permissionDecision")


def state_file(world, session_id) -> dict:
    root = Path(os.environ["XDG_STATE_HOME"]) / "vaultlines" / "sessions"
    return json.loads((root / f"{session_id}.json").read_text())


@pytest.fixture
def live(world):
    write_runtime(world)
    return world


def test_a_session_and_its_subagent_share_one_label(live):
    w = live
    sid = "f427a213-8b5f-4eb3-96e9-2571f40724d1"
    out = hook(w, start(sid, w.acme))
    assert "acme-founders" in out["hookSpecificOutput"]["additionalContext"]
    assert decision(hook(w, tool(sid, w.acme, "Read", {"file_path": w.vault("acme-founders", "plan.md")}))) is None
    assert state_file(w, sid)["read"] == ["acme-founders"]
    # A subagent: same session_id, plus agent_id and agent_type.
    out = hook(w, tool(sid, w.acme, "Write", {"file_path": w.vault("acme-everyone", "x.md"), "content": "hi"},
                       agent_id="aa5903a262d114864", agent_type="general-purpose"))
    assert decision(out) == "ask"
    assert "ana" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_fork_gets_a_new_id_and_starts_as_only_you(live):
    w = live
    hook(w, start("parent", w.site))
    hook(w, start("forked", w.site, source="fork"))
    assert state_file(w, "forked")["label"] == []
    assert decision(hook(w, tool("forked", w.site, "Write", {"file_path": w.vault("acme-everyone", "n.md")}))) == "ask"
    assert decision(hook(w, tool("parent", w.site, "Write", {"file_path": w.vault("acme-everyone", "n.md")}))) is None


def test_basic_memory_rewrite(live):
    w = live
    hook(w, start("bm", w.site))
    out = hook(w, tool("bm", w.site, "mcp__basic-memory__search_notes", {"query": "launch"}))
    assert out["hookSpecificOutput"]["updatedInput"] == {"query": "launch", "project": "acme-everyone"}


def test_other_tools_print_nothing(live):
    w = live
    assert hook(w, tool("x", w.home, "WebSearch", {"query": "hi"})) is None
    assert hook(w, {"hook_event_name": "Stop", "session_id": "x"}) is None


def test_the_briefing_is_read_from_the_plugin_settings(live):
    w = live
    (w.acme / ".claude").mkdir()
    (w.acme / ".claude" / "settings.local.json").write_text(json.dumps({"basicMemory": {"primaryProject": "acme-founders"}}))
    hook(w, start("brief", w.api), project_dir=w.api)
    assert state_file(w, "brief")["read"] == ["acme-founders"]


def test_errors_fail_closed_only_for_vault_calls(live):
    w = live
    from vaultlines.hook import runtime_path

    runtime_path().write_text("{not json")
    out = hook(w, tool("e", w.acme, "Read", {"file_path": w.vault("acme-founders", "a.md")}))
    assert decision(out) == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("vl hook error: run `vl doctor`")
    assert decision(hook(w, tool("e", w.acme, "Read", {"file_path": str(w.acme / "README.md")}))) == "deny"
    assert hook(w, start("e", w.acme)) is None
    # A crash inside decide() only blocks calls that may touch a vault.
    w.runtime["folders"][str(w.acme)] = "not a table"
    write_runtime(w)
    assert decision(hook(w, tool("e", w.acme, "Read", {"file_path": w.vault("acme-founders", "a.md")}))) == "deny"
    assert hook(w, tool("e", w.acme, "Read", {"file_path": str(w.acme / "README.md")})) is None
    # No runtime.json at all: vl isn't set up, so only Basic Memory calls are refused.
    runtime_path().unlink()
    assert decision(hook(w, tool("e", w.acme, "mcp__basic-memory__read_note", {"identifier": "x"}))) == "deny"
    assert hook(w, tool("e", w.acme, "Read", {"file_path": w.vault("acme-founders", "a.md")})) is None


def test_bad_stdin_is_harmless(live):
    result = subprocess.run([VL, "hook"], input="not json", capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == ""


def test_parallel_calls_dont_lose_reads(live):
    w = live
    hook(w, start("par", w.side_project))
    events = [tool("par", w.side_project, "Read", {"file_path": w.vault(v, "x.md")})
              for v in ("acme-founders", "acme", "side", "handbook")]
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(w.side_project)}
    procs = [subprocess.Popen([VL, "hook"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
             for _ in events]
    for p, e in zip(procs, events):
        p.stdin.write(json.dumps(e))
        p.stdin.close()
    for p in procs:
        p.wait()
    assert sorted(state_file(w, "par")["read"]) == ["acme", "acme-founders", "handbook", "side"]


def test_speed(live):
    w = live
    hook(w, start("speed", w.acme))
    event = json.dumps(tool("speed", w.acme, "Read", {"file_path": w.vault("acme-everyone", "a.md")}))
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(w.acme)}
    runs = 10
    t = time.perf_counter()
    for _ in range(runs):
        subprocess.run([VL, "hook"], input=event, capture_output=True, text=True, env=env)
    per_call = (time.perf_counter() - t) / runs * 1000
    print(f"vl hook: {per_call:.0f} ms per call")
    assert per_call < 100
