#!/bin/bash
# End-to-end test on two fake computers ("alice" and "bob") with local git remotes
# standing in for GitHub, and a fake list of who can see each one.
# Touches nothing outside a temporary folder. Needs git, jq, uv and claude.
#
#   tests/e2e.sh            run and clean up
#   KEEP=1 tests/e2e.sh     keep the temporary folder to look around
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(mktemp -d)"
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
    VAULTLINES_TEST_REMOTES=1 VAULTLINES_FAKE_AUDIENCE="$(jq -c --arg me "$who" '. + {me: $me}' <<<"$AUDIENCES")" \
    UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}" HF_HOME="${HF_HOME:-$REAL_HOME/.cache/huggingface}" \
    GIT_CONFIG_GLOBAL="$ROOT/$who/.gitconfig" "$@"
}
vl() { local who="$1"; shift; as "$who" uv run --quiet --project "$REPO" vl "$@"; }
bmtool() {
  local who="$1" out; shift
  out="$(as "$who" uvx basic-memory tool "$@" 2>&1)" || { echo "$out" | tail -5; return 1; }
}
servers() { jq -r --arg f "$2" '.projects[$f].mcpServers // {} | keys | join(",")' "$ROOT/$1/.claude/.claude.json"; }
user_servers() { jq -r '.mcpServers // {} | keys | join(",")' "$ROOT/$1/.claude/.claude.json"; }

for who in alice bob; do git config --file "$ROOT/$who/.gitconfig" init.defaultBranch main; done
for r in acme-founders acme-everyone workspace; do git init -q --bare -b main "$ROOT/remotes/$r.git"; done

echo "== alice: init, two team vaults, two folders"
vl alice init --local >/dev/null
vl alice vault join "$R/acme-founders.git" >/dev/null
vl alice vault join "$R/acme-everyone.git" >/dev/null
vl alice sync >/dev/null
LEGAL="$(cd "$ROOT/alice" && mkdir -p work/legal && cd work/legal && git init -q && pwd -P)"
APP="$(cd "$ROOT/alice" && mkdir -p work/app && cd work/app && git init -q && pwd -P)"
vl alice folder set "$LEGAL" --writes acme-founders --reads acme-everyone >/dev/null
vl alice folder set "$APP" --writes acme-everyone >/dev/null

check "personal vault is available everywhere" test "$(user_servers alice)" = "vl-personal"
check "legal folder: founders + everyone servers" test "$(servers alice "$LEGAL")" = "vl-acme-everyone,vl-acme-founders"
check "app folder: everyone server only" test "$(servers alice "$APP")" = "vl-acme-everyone"
check "legal folder blocks personal" jq -e '.permissions.deny == ["mcp__vl-personal"]' "$LEGAL/.claude/settings.local.json"
check "legal folder asks before writing everyone" jq -e '.permissions.ask | index("mcp__vl-acme-everyone__write_note")' "$LEGAL/.claude/settings.local.json"
check "legal folder writes to acme-founders" jq -e '.basicMemory.primaryProject == "acme-founders"' "$LEGAL/.claude/settings.local.json"
check "admin tools denied at user level" jq -e '.permissions.deny | index("mcp__vl-acme-founders__delete_project")' "$ROOT/alice/.claude/settings.json"
check "settings.local.json kept out of git" test -z "$(git -C "$LEGAL" status --porcelain)"

BEFORE="$(jq -S '{m: .mcpServers, p: (.projects | map_values(.mcpServers))}' "$ROOT/alice/.claude/.claude.json")"
vl alice apply >/dev/null
AFTER="$(jq -S '{m: .mcpServers, p: (.projects | map_values(.mcpServers))}' "$ROOT/alice/.claude/.claude.json")"
check "apply twice changes nothing" test "$BEFORE" = "$AFTER"

echo "== the audience check refuses unsafe folders"
OUT="$(vl alice folder set "$APP" --writes acme-everyone --reads acme-founders 2>&1 || true)"
check "folder set refuses reading founders from everyone" grep -q "bob can see acme-everyone but not acme-founders" <<<"$OUT"
check "  ...and changes nothing" test "$(servers alice "$APP")" = "vl-acme-everyone"
OUT="$(vl alice folder set "$APP" --writes acme-everyone --reads personal 2>&1 || true)"
check "folder set refuses reading a local vault from a shared one" grep -q "bob and carol can see acme-everyone but not personal" <<<"$OUT"

CONFIG="$ROOT/alice/.config/vaultlines/config.toml"
perl -0pi -e 's/(\[folders\."~\/work\/app"\]\nwrites = "acme-everyone"\nreads = )\[\]/$1\["acme-founders"\]/' "$CONFIG"
check "(config edited by hand)" grep -q 'reads = \[ *"acme-founders" *\]' "$CONFIG"
refuses "apply exits with an error" vl alice apply
check "the refused folder isn't set up, so it falls back to \"*\"" test -z "$(servers alice "$APP")"
check "  ...other folders are still set up" test "$(servers alice "$LEGAL")" = "vl-acme-everyone,vl-acme-founders"
refuses "check exits with an error" vl alice check
check "status shows the refusal" grep -q "REFUSED" <<<"$(vl alice status)"
vl alice folder set "$APP" --writes acme-everyone >/dev/null
check "fixing the folder sets it up again" test "$(servers alice "$APP")" = "vl-acme-everyone"

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
mkdir -p "$ROOT/alice/plain"
refuses "auto_pull needs a git repo" vl alice folder set "$ROOT/alice/plain" --writes personal --auto-pull
refuses 'auto_pull not allowed on "*"' vl alice folder set "*" --writes personal --auto-pull

echo "== a change on GitHub is caught by the daily check"
AUDIENCES="$(jq -c --arg r "$R" '.[$r + "/acme-founders.git"] += ["dan"]' <<<"$AUDIENCES")"
STATE="$ROOT/alice/.config/vaultlines/state.json"
OUT="$(vl alice sync --background 2>&1)"
check "no re-check within a day" test -z "$(grep 'check:' <<<"$OUT" || true)"
check "  ...so the legal folder is still set up" test "$(servers alice "$LEGAL")" = "vl-acme-everyone,vl-acme-founders"
jq '.checked_at = 0' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"   # a day passes
OUT="$(vl alice sync --background 2>&1)"
check "the daily check reports the new problem" grep -q "check: ~/work/legal can't read acme-everyone and write acme-founders: dan can see acme-founders but not acme-everyone" <<<"$OUT"
check "  ...and takes the folder down" test -z "$(servers alice "$LEGAL")"
check "  ...and remembers it, so it notifies only once" jq -e '.refused | length == 1' "$STATE"
jq '.checked_at = 0' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
OUT="$(vl alice sync --background 2>&1)"
check "the next daily check doesn't report it again" test -z "$(grep 'check:' <<<"$OUT" || true)"
AUDIENCES="$(jq -c --arg r "$R" '.[$r + "/acme-everyone.git"] += ["dan"]' <<<"$AUDIENCES")"
vl alice apply >/dev/null
check "once dan can see both, apply sets it up again" test "$(servers alice "$LEGAL")" = "vl-acme-everyone,vl-acme-founders"

echo "== adopt, unset, remove"
mkdir -p "$ROOT/alice/old-notes" && echo "# Old" > "$ROOT/alice/old-notes/old.md"
vl alice vault adopt "$ROOT/alice/old-notes" >/dev/null
check "adopted folder became a git repo" test -d "$ROOT/alice/old-notes/.git"
vl alice folder unset "$APP" >/dev/null
check "unset removed the folder's servers" test -z "$(servers alice "$APP")"
check "unset removed the folder's vault settings" jq -e '.basicMemory == null' "$APP/.claude/settings.local.json"
refuses "can't remove a vault a folder still uses" vl alice vault remove acme-founders
vl bob vault remove acme-everyone >/dev/null
check "remove kept the files" test -f "$ROOT/bob/Vaults/acme-everyone/notes/From bob.md"
check "doctor passes for alice" vl alice doctor

echo "All end-to-end checks passed."
