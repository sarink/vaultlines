#!/bin/bash
# End-to-end test on two fake computers ("alice" and "bob") with local git remotes
# standing in for GitHub, and a fake list of who can see each one.
# Touches nothing outside a temporary folder. Needs git, jq, uv and claude.
#
#   tests/e2e.sh            run and clean up
#   KEEP=1 tests/e2e.sh     keep the temporary folder to look around
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$(mktemp -d)" && pwd -P)"
REAL_HOME="$HOME"
trap '[ "${KEEP:-}" = 1 ] && echo "kept: $ROOT" || rm -rf "$ROOT"' EXIT
trap 'echo "  FAIL  command on line $LINENO exited with an error"' ERR

pass() { echo "  ok    $*"; }
fail() { echo "  FAIL  $*"; exit 1; }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi; }
refuses() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$what"; else pass "$what"; fi; }

mkdir -p "$ROOT/remotes" "$ROOT/alice" "$ROOT/bob"
R="file://$ROOT/remotes"

# Who can see each remote, as GitHub would report it. Change it to simulate GitHub changes.
AUDIENCES="$(jq -nc --arg r "$R" '{
  ($r + "/acme-founders.git"): ["alice", "carol"],
  ($r + "/acme-everyone.git"): ["alice", "bob", "carol"]
}')"

# Run vl (or any command) as one of the fake computers.
as() {
  local who="$1"; shift
  HOME="$ROOT/$who" CLAUDE_CONFIG_DIR="$ROOT/$who/.claude" VAULTLINES_NO_LAUNCHD=1 VAULTLINES_NO_NOTIFY=1 \
    XDG_CONFIG_HOME="$ROOT/$who/.config" XDG_STATE_HOME="$ROOT/$who/.local/state" \
    VAULTLINES_TEST_REMOTES=1 VAULTLINES_FAKE_AUDIENCE="$(jq -c --arg me "$who" '. + {me: $me}' <<<"$AUDIENCES")" \
    UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}" HF_HOME="${HF_HOME:-$REAL_HOME/.cache/huggingface}" \
    GIT_CONFIG_GLOBAL="$ROOT/$who/.gitconfig" "$@"
}
vl() { local who="$1"; shift; as "$who" uv run --quiet --project "$REPO" vl "$@"; }
bmtool() {
  local who="$1" out; shift
  out="$(as "$who" uvx basic-memory tool "$@" 2>&1)" || { echo "$out" | tail -5; return 1; }
}
user_servers() { jq -r '.mcpServers // {} | keys | join(",")' "$ROOT/$1/.claude/.claude.json"; }
folder_servers() { jq -r --arg f "$2" '.projects[$f].mcpServers // {} | keys | join(",")' "$ROOT/$1/.claude/.claude.json"; }
runtime() { cat "$ROOT/$1/.local/state/vaultlines/runtime.json"; }
# Feed one hook event to `vl hook` as Claude Code would, and print the decision.
hook() {
  local who="$1" cwd="$2" event="$3" out
  out="$(jq -c --arg cwd "$cwd" '. + {cwd: $cwd, session_id: (.session_id // "e2e-session")}' <<<"$event" \
    | CLAUDE_PROJECT_DIR="$cwd" vl "$who" hook)"
  if [ -z "$out" ]; then echo allow; return; fi
  jq -r '.hookSpecificOutput.permissionDecision // .hookSpecificOutput.additionalContext // "allow"' <<<"$out"
}
write_event() { jq -nc --arg p "$1" '{hook_event_name: "PreToolUse", tool_name: "Write", tool_input: {file_path: $p, content: "x"}}'; }
read_event() { jq -nc --arg p "$1" '{hook_event_name: "PreToolUse", tool_name: "Read", tool_input: {file_path: $p}}'; }

for who in alice bob; do git config --file "$ROOT/$who/.gitconfig" init.defaultBranch main; done
for r in acme-founders acme-everyone workspace; do git init -q --bare -b main "$ROOT/remotes/$r.git"; done

echo "== alice: init, two team vaults, two folders"
vl alice init --local >/dev/null
CONFIG="$ROOT/alice/.config/vaultlines/config.toml"
check "init lists ~ with the personal vault" grep -q '^\[folders."~"\]' "$CONFIG"
check "init turns the Basic Memory plugin on" grep -q '^\[plugins.basic-memory\]' "$CONFIG"
check "  ...with its kind" grep -q '^kind = "basic-memory"$' "$CONFIG"
check "hooks installed for SessionStart and PreToolUse" \
  jq -e '[.hooks.SessionStart[].hooks[].command, .hooks.PreToolUse[].hooks[].command] | map(endswith("vl hook")) | all and length == 2' "$ROOT/alice/.claude/settings.json"
check "  ...and UserPromptSubmit" jq -e '.hooks.UserPromptSubmit[0].hooks[0].command | endswith("vl hook")' "$ROOT/alice/.claude/settings.json"
check "one basic-memory server, at user level" test "$(user_servers alice)" = "basic-memory"
check "the server isn't locked to one project" jq -e '.mcpServers["basic-memory"].args == ["basic-memory", "mcp"]' "$ROOT/alice/.claude/.claude.json"
check "plugin writes to personal at user level" jq -e '.basicMemory.primaryProject == "personal"' "$ROOT/alice/.claude/settings.json"

vl alice vault join "$R/acme-founders.git" >/dev/null
vl alice vault join "$R/acme-everyone.git" >/dev/null
vl alice sync >/dev/null
LEGAL="$(cd "$ROOT/alice" && mkdir -p work/legal && cd work/legal && git init -q && pwd -P)"
APP="$(cd "$ROOT/alice" && mkdir -p work/app && cd work/app && git init -q && pwd -P)"
vl alice folder set "$LEGAL" --writes acme-founders --reads acme-everyone >/dev/null
vl alice folder set "$APP" --writes acme-everyone >/dev/null

check "legal folder's plugin block writes to acme-founders" jq -e '.basicMemory.primaryProject == "acme-founders"' "$LEGAL/.claude/settings.local.json"
check "  ...with checkpoints kept local" jq -e '.basicMemory.captureFolder == "sessions" and .basicMemory.captureEvents == false' "$LEGAL/.claude/settings.local.json"
check "settings.local.json kept out of git" test -z "$(git -C "$LEGAL" status --porcelain)"
check "no per-folder servers any more" test -z "$(folder_servers alice "$LEGAL")"
check "runtime.json has the folders" jq -e --arg l "$LEGAL" '.folders[$l] == {"writes": "acme-founders", "reads": ["acme-everyone"]}' <(runtime alice)
check "runtime.json has who can see each vault" jq -e '.vaults["acme-founders"].audience.logins == ["alice", "carol"] and .me == "alice"' <(runtime alice)
check "runtime.json maps Basic Memory projects to vaults" jq -e '.plugins["basic-memory"].data.projects["acme-everyone"] == "acme-everyone"' <(runtime alice)

snapshot() { jq -S '{m: .mcpServers, p: (.projects // {} | map_values(.mcpServers))}' "$ROOT/alice/.claude/.claude.json"; cat "$ROOT/alice/.claude/settings.json" "$LEGAL/.claude/settings.local.json"; }
BEFORE="$(snapshot)"
vl alice apply >/dev/null
check "apply twice changes nothing" test "$BEFORE" = "$(snapshot)"

echo "== the hook guards what the session reads and writes"
FOUNDERS="$ROOT/alice/Vaults/acme-founders"
EVERYONE="$ROOT/alice/Vaults/acme-everyone"
check "session start tells Claude where to save" grep -q 'save notes from this folder to the `acme-founders` vault' \
  <<<"$(hook alice "$LEGAL" '{"hook_event_name": "SessionStart", "source": "startup"}')"
check "writing founders after reading everyone is allowed" test "$(hook alice "$LEGAL" "$(read_event "$EVERYONE/a.md")"; hook alice "$LEGAL" "$(write_event "$FOUNDERS/b.md")")" = "$(printf 'allow\nallow')"
check "writing everyone (a read vault) asks" test "$(hook alice "$LEGAL" "$(write_event "$EVERYONE/b.md")")" = "ask"
check "the app folder can't use acme-founders" test "$(hook alice "$APP" "$(read_event "$FOUNDERS/b.md" | jq -c '. + {session_id: "app"}')")" = "deny"
check "Basic Memory calls get the folder's vault" \
  grep -q '"project": "acme-everyone"' <<<"$(jq -nc --arg cwd "$APP" '{hook_event_name: "PreToolUse", session_id: "bm", cwd: $cwd, tool_name: "mcp__basic-memory__search_notes", tool_input: {query: "x"}}' | CLAUDE_PROJECT_DIR="$APP" vl alice hook)"
VLCMD='{"hook_event_name": "PreToolUse", "session_id": "guard", "tool_name": "Bash", "tool_input": {"command": "vl folder set . --reads acme-founders"}}'
hook alice "$APP" '{"hook_event_name": "UserPromptSubmit", "session_id": "guard", "prompt": "tidy the notes"}' >/dev/null
check "Claude running vl folder set on its own asks" test "$(hook alice "$APP" "$VLCMD")" = "ask"
hook alice "$APP" '{"hook_event_name": "UserPromptSubmit", "session_id": "guard", "prompt": "run vl folder set for me"}' >/dev/null
check "  ...but not when you asked for vl" test "$(hook alice "$APP" "$VLCMD")" = "allow"
check "vl's session records can't be written" test "$(hook alice "$APP" "$(write_event "$ROOT/alice/.local/state/vaultlines/runtime.json")")" = "deny"
check "vl sessions shows the session's label" grep -q "acme-everyone" <<<"$(vl alice sessions)"

echo "== reads that would leak ask, and check says where"
vl alice folder set "$APP" --writes acme-everyone --reads acme-founders >/dev/null
check "folder set no longer refuses; check previews the ask" \
  grep -q "writes to acme-everyone ask after reading acme-founders (bob can't see acme-founders)" <<<"$(vl alice check)"
hook alice "$APP" '{"hook_event_name": "SessionStart", "source": "startup", "session_id": "leak"}' >/dev/null
hook alice "$APP" "$(read_event "$FOUNDERS/b.md" | jq -c '. + {session_id: "leak"}')" >/dev/null
check "reading founders then writing everyone asks" test "$(hook alice "$APP" "$(write_event "$EVERYONE/c.md" | jq -c '. + {session_id: "leak"}')")" = "ask"
mkdir -p "$APP/.claude" && echo '{"disableAllHooks": true}' > "$APP/.claude/settings.json"
check "check warns about disableAllHooks" grep -q "sets disableAllHooks" <<<"$(vl alice check 2>&1)"
check "status warns too" grep -q "sets disableAllHooks" <<<"$(vl alice status 2>&1)"
rm "$APP/.claude/settings.json"
vl alice folder set "$APP" --writes acme-everyone >/dev/null
refuses '"*" is gone' vl alice folder set "*" --writes personal

echo "== vaultlines 0.2 leftovers are removed"
as alice claude mcp add -s user vl-personal -- uvx basic-memory mcp --project personal >/dev/null
(cd "$LEGAL" && as alice claude mcp add -s local vl-acme-founders -- uvx basic-memory mcp --project acme-founders >/dev/null)
jq '.permissions = {ask: ["mcp__vl-acme-everyone__write_note"], deny: ["mcp__vl-personal", "Bash(rm:*)"]} | .basicMemory.secondaryProjects = ["acme-everyone"]' \
  "$LEGAL/.claude/settings.local.json" > "$LEGAL/tmp.json" && mv "$LEGAL/tmp.json" "$LEGAL/.claude/settings.local.json"
vl alice apply >/dev/null
check "vl-* user servers removed" test "$(user_servers alice)" = "basic-memory"
check "vl-* folder servers removed" test -z "$(folder_servers alice "$LEGAL")"
check "mcp__vl-* rules removed, others kept" jq -e '.permissions == {deny: ["Bash(rm:*)"]}' "$LEGAL/.claude/settings.local.json"
check "old secondaryProjects replaced" jq -e '.basicMemory.secondaryProjects == null' "$LEGAL/.claude/settings.local.json"

echo "== bob: joins the everyone vault; notes flow both ways"
vl bob init --local >/dev/null
vl bob vault join "$R/acme-everyone.git" >/dev/null
bmtool bob write-note --title "From bob" --folder notes --content "- [fact] hello from bob" --project acme-everyone
bmtool bob write-note --title "Bob checkpoint" --folder sessions --content "- [x] private" --project acme-everyone
vl bob sync >/dev/null
vl alice sync >/dev/null
check "alice got bob's note" test -f "$ROOT/alice/Vaults/acme-everyone/notes/From bob.md"
check "bob's sessions/ stayed on bob's computer" test ! -e "$ROOT/alice/Vaults/acme-everyone/sessions/Bob checkpoint.md"

printf '\n- [fact] alice line\n' >> "$ROOT/alice/Vaults/acme-everyone/notes/From bob.md"
printf '\n- [fact] bob line\n' >> "$ROOT/bob/Vaults/acme-everyone/notes/From bob.md"
vl alice sync >/dev/null && vl bob sync >/dev/null && vl alice sync >/dev/null
check "edits to the same note from both keep both lines" \
  grep -q "bob line" "$ROOT/alice/Vaults/acme-everyone/notes/From bob.md"
check "  ...and alice's line too" grep -q "alice line" "$ROOT/alice/Vaults/acme-everyone/notes/From bob.md"
check "bob never sees the founders vault" test ! -e "$ROOT/bob/Vaults/acme-founders"

echo "== auto_pull keeps a workspace repo up to date"
SEED="$ROOT/seed"
git clone -q "$R/workspace.git" "$SEED" 2>/dev/null
(cd "$SEED" && echo "# Team" > CLAUDE.md && git add -A && git -c user.name=t -c user.email=t@t commit -qm one && git push -q origin HEAD:main)
git clone -q "$R/workspace.git" "$ROOT/alice/work/ws"
WS="$(cd "$ROOT/alice/work/ws" && pwd -P)"
vl alice folder set "$WS" --writes acme-everyone --auto-pull >/dev/null
check "config has auto_pull" grep -q "auto_pull = true" "$CONFIG"
(cd "$SEED" && echo "skill" > skill.md && git add -A && git -c user.name=t -c user.email=t@t commit -qm two && git push -q origin HEAD:main)
vl alice sync >/dev/null
check "sync pulled the new commit" test -f "$WS/skill.md"
check "  ...and the plugin block stays out of that repo's git" test -z "$(git -C "$WS" status --porcelain)"
mkdir -p "$ROOT/alice/plain"
refuses "auto_pull needs a git repo" vl alice folder set "$ROOT/alice/plain" --writes personal --auto-pull

echo "== a change on GitHub is caught by the daily check"
AUDIENCES="$(jq -c --arg r "$R" '.[$r + "/acme-founders.git"] += ["dan"]' <<<"$AUDIENCES")"
STATE="$ROOT/alice/.config/vaultlines/state.json"
vl alice sync --background >/dev/null 2>&1
check "no re-check within check_interval" jq -e '.vaults["acme-founders"].audience.logins | index("dan") | not' <(runtime alice)
jq '.checked_at = 0' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"   # a day passes
mkdir -p "$APP/.claude" && echo '{"disableAllHooks": true}' > "$APP/.claude/settings.local.json"
OUT="$(vl alice sync --background 2>&1)"
check "the daily check updates who can see each vault" jq -e '.vaults["acme-founders"].audience.logins | index("dan")' <(runtime alice)
check "  ...and reports new disableAllHooks files" grep -q "check: .*sets disableAllHooks" <<<"$OUT"
jq '.checked_at = 0' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
OUT="$(vl alice sync --background 2>&1)"
check "  ...only once" test -z "$(grep 'check:' <<<"$OUT" || true)"
rm "$APP/.claude/settings.local.json"
hook alice "$LEGAL" '{"hook_event_name": "SessionStart", "source": "startup", "session_id": "dan"}' >/dev/null
hook alice "$LEGAL" "$(read_event "$EVERYONE/a.md" | jq -c '. + {session_id: "dan"}')" >/dev/null
OUT="$(write_event "$FOUNDERS/c.md" | jq -c --arg cwd "$LEGAL" '. + {session_id: "dan", cwd: $cwd}' | CLAUDE_PROJECT_DIR="$LEGAL" vl alice hook)"
check "now writing founders after reading everyone asks" jq -e '.hookSpecificOutput.permissionDecision == "ask"' <<<"$OUT"
check "  ...naming dan" jq -e '.hookSpecificOutput.permissionDecisionReason | contains("dan would see this in acme-founders")' <<<"$OUT"

echo "== old sessions are cleaned up"
SESSIONS="$ROOT/alice/.local/state/vaultlines/sessions"
touch -t 202001010000 "$SESSIONS/e2e-session.json"
vl alice sync >/dev/null
check "sync deletes session files older than 30 days" test ! -e "$SESSIONS/e2e-session.json"
check "  ...and keeps new ones" test -e "$SESSIONS/leak.json"

echo "== adopt, unset, remove, uninstall"
mkdir -p "$ROOT/alice/old-notes" && echo "# Old" > "$ROOT/alice/old-notes/old.md"
vl alice vault adopt "$ROOT/alice/old-notes" >/dev/null
check "adopted folder became a git repo" test -d "$ROOT/alice/old-notes/.git"
vl alice folder unset "$APP" >/dev/null
check "unset removed the folder's plugin block" jq -e '.basicMemory == null' "$APP/.claude/settings.local.json"
check "  ...and its runtime entry" jq -e --arg a "$APP" '.folders[$a] == null' <(runtime alice)
refuses "can't remove a vault a folder still uses" vl alice vault remove acme-founders
vl bob vault remove acme-everyone >/dev/null
check "remove kept the files" test -f "$ROOT/bob/Vaults/acme-everyone/notes/From bob.md"
check "doctor passes for alice" vl alice doctor
sed '/^\[plugins.basic-memory\]/,/^$/d' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
vl alice apply >/dev/null
check "turning the plugin off removes its folder blocks" jq -e '.basicMemory == null' "$LEGAL/.claude/settings.local.json"
check "  ...and the user-level one" jq -e '.basicMemory == null' "$ROOT/alice/.claude/settings.json"
check "  ...and its runtime entry" jq -e '.plugins == {}' <(runtime alice)
check "  ...and doctor still passes" vl alice doctor
vl alice uninstall >/dev/null
check "uninstall removed the hooks" jq -e '.hooks == null' "$ROOT/alice/.claude/settings.json"
refuses "  ...so doctor fails" vl alice doctor

echo "All end-to-end checks passed."
