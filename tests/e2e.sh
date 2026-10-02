#!/bin/bash
# End-to-end test on two fake computers, "alice" and "bob", in the fake GitHub org mixim-ai.
# A JSON file and bare repos stand in for GitHub, a local server for Google, and a local
# folder for the shared drive. Touches nothing outside a temporary folder. Needs git, jq,
# uv and claude (and rclone for the Drive section, which is skipped without it).
#
#   tests/e2e.sh            run and clean up
#   KEEP=1 tests/e2e.sh     keep the temporary folder to look around
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$(mktemp -d)" && pwd -P)"
REAL_HOME="$HOME"
GOOGLE_PID=""
trap '[ -n "$GOOGLE_PID" ] && kill "$GOOGLE_PID" 2>/dev/null; [ "${KEEP:-}" = 1 ] && echo "kept: $ROOT" || rm -rf "$ROOT"' EXIT
trap 'echo "  FAIL  command on line $LINENO exited with an error"' ERR

pass() { echo "  ok    $*"; }
fail() { echo "  FAIL  $*"; exit 1; }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi; }
refuses() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$what"; else pass "$what"; fi; }

GH="$ROOT/github.json"
R="$ROOT/remotes"
mkdir -p "$R" "$ROOT/alice" "$ROOT/bob"
GOOGLE="http://127.0.0.1:9"  # set below, once the fake Google runs

# Run vl (or any command) as one of the fake computers.
as() {
  local who="$1"; shift
  HOME="$ROOT/$who" VAULTLINES_HOME="$ROOT/$who/.vaultlines" CLAUDE_CONFIG_DIR="$ROOT/$who/.claude" \
    VAULTLINES_NO_LAUNCHD=1 VAULTLINES_NO_NOTIFY=1 VAULTLINES_TEST_REMOTES=1 \
    VAULTLINES_FAKE_GITHUB="$GH" VAULTLINES_FAKE_LOGIN="$who" VAULTLINES_FAKE_GOOGLE="$GOOGLE" \
    UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}" HF_HOME="${HF_HOME:-$REAL_HOME/.cache/huggingface}" \
    GIT_CONFIG_GLOBAL="$ROOT/$who/.gitconfig" "$@"
}
vl() { local who="$1"; shift; as "$who" uv run --quiet --project "$REPO" vl "$@"; }
bmtool() {
  local who="$1" out; shift
  out="$(as "$who" uvx basic-memory tool "$@" 2>&1)" || { echo "$out" | tail -5; return 1; }
}
runtime() { cat "$ROOT/$1/.vaultlines/state/runtime.json"; }
session() { cat "$ROOT/$1/.vaultlines/state/sessions/$2.json"; }

# Feed one hook event to `vl hook` as Claude Code would, and print the decision.
hook() {
  local who="$1" cwd="$2" event="$3" out
  out="$(jq -c --arg cwd "$cwd" '. + {cwd: $cwd, session_id: (.session_id // "e2e-session")}' <<<"$event" \
    | (cd "$cwd" && CLAUDE_PROJECT_DIR="$cwd" vl "$who" hook))"
  if [ -z "$out" ]; then echo allow; return; fi
  jq -r '.hookSpecificOutput.permissionDecision // .hookSpecificOutput.additionalContext // "allow"' <<<"$out"
}
start() { hook "$1" "$2" "$(jq -nc --arg s "$3" '{hook_event_name: "SessionStart", source: "startup", session_id: $s}')"; }
writes_of() { start "$1" "$2" "$3" >/dev/null; session "$1" "$3" | jq -r '.rules.writes'; }
write_event() { jq -nc --arg p "$1" --arg s "$2" '{hook_event_name: "PreToolUse", session_id: $s, tool_name: "Write", tool_input: {file_path: $p, content: "x"}}'; }
read_event() { jq -nc --arg p "$1" --arg s "$2" '{hook_event_name: "PreToolUse", session_id: $s, tool_name: "Read", tool_input: {file_path: $p}}'; }
bash_event() { jq -nc --arg c "$1" --arg s "$2" '{hook_event_name: "PreToolUse", session_id: $s, tool_name: "Bash", tool_input: {command: $c}}'; }

# ---------------------------------------------------------------- a fake GitHub

for who in alice bob; do git config --file "$ROOT/$who/.gitconfig" init.defaultBranch main; done
git config --file "$ROOT/admin.gitconfig" init.defaultBranch main
git config --file "$ROOT/admin.gitconfig" user.name admin
git config --file "$ROOT/admin.gitconfig" user.email admin@example.com
admin() { GIT_CONFIG_GLOBAL="$ROOT/admin.gitconfig" git "$@"; }
jq -n --arg r "$R" '{root: $r, orgs: {"mixim-ai": ["alice", "bob"]}, repos: {}}' > "$GH"
# repo OWNER/REPO ACCESS [FILE TEXT]...: a repo on the fake GitHub, with these files.
repo() {
  local id="$1" access="$2"; shift 2
  jq --arg id "$id" --argjson a "$access" '.repos[$id] = $a' "$GH" > "$GH.tmp" && mv "$GH.tmp" "$GH"
  admin init -q --bare "$R/$id.git"
  admin clone -q "$R/$id.git" "$ROOT/admin/$id" 2>/dev/null
  while [ $# -gt 0 ]; do mkdir -p "$(dirname "$ROOT/admin/$id/$1")"; printf '%s' "$2" > "$ROOT/admin/$id/$1"; shift 2; done
  admin -C "$ROOT/admin/$id" add -A && admin -C "$ROOT/admin/$id" commit -qm seed && admin -C "$ROOT/admin/$id" push -q origin HEAD:main
}
publish() {  # publish OWNER/REPO MESSAGE: push what the admin changed
  admin -C "$ROOT/admin/$1" add -A && admin -C "$ROOT/admin/$1" commit -qm "$2"
  admin -C "$ROOT/admin/$1" pull -q --rebase origin main && admin -C "$ROOT/admin/$1" push -q origin HEAD:main
}
BOTH='{"push": ["alice", "bob"]}'
repo mixim-ai/vault-public "$BOTH" vault.toml 'about      = "Notes everyone at Mixim can see."
notes_from = ["mixim-ai/marketing", "mixim-ai/mixim-workspace", "mixim-ai/both"]
'
repo mixim-ai/vault-private '{"push": ["alice"]}' vault.toml 'about      = "Founders notes: fundraising, hiring, legal."
notes_from = ["mixim-ai/jorge-ip-theft", "mixim-ai/both"]
'
DRIVE="$ROOT/drive"
mkdir -p "$DRIVE/Team Docs" "$DRIVE/Finance"
echo "# Plan" > "$DRIVE/Team Docs/Plan.md"
cp "$REPO/tests/fixtures/sample.xlsx" "$DRIVE/Finance/Runway.xlsx"
printf 'PK\005\006' > "$DRIVE/old.zip"
repo mixim-ai/vault-hq '{"push": ["alice"], "read": ["bob"]}' vault.toml "about = \"The text of every file in the Mixim HQ shared drive. Claude only reads it.\"

[source]
kind                 = \"gdrive\"
shared_drive         = \"$DRIVE\"
folder               = \"\"
max_size             = \"50M\"
google_client_id     = \"1234-abc.apps.googleusercontent.com\"
google_client_secret = \"GOCSPX-x\"
"
repo mixim-ai/vault-notes "$BOTH" README.md 'no vault.toml, so not a vault'
for code in marketing sheety mixim-workspace both studio; do repo "mixim-ai/$code" "$BOTH" README.md "# $code"; done
repo mixim-ai/jorge-ip-theft '{"push": ["alice"]}' README.md '# case'
repo alice/blog '{"push": ["alice"]}' README.md '# blog'

# ---------------------------------------------------------------- a fake Google
GOOGLE_FILES="$ROOT/google-files.json"
jq -n '{F1: {name: "Runway.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data: "PK original"},
        F403: {name: "Secret.pdf", mimeType: "application/pdf", status: 403}}' > "$GOOGLE_FILES"
uv run --quiet --project "$REPO" python "$REPO/tests/fake_google.py" "$GOOGLE_FILES" > "$ROOT/google.url" &
GOOGLE_PID=$!
for _ in $(seq 50); do [ -s "$ROOT/google.url" ] && break; sleep 0.1; done
GOOGLE="$(cat "$ROOT/google.url")"

echo "== alice joins mixim-ai: one command sets everything up"
vl alice org join mixim-ai >/dev/null
V="$ROOT/alice/.vaultlines/vaults"
check "vl init ran first: config.toml is comments only" test -z "$(grep -v '^#' "$ROOT/alice/.vaultlines/config.toml" | grep -v '^$' || true)"
check "your personal vault, on this computer only" test -z "$(git -C "$V/alice/vault-alice-personal" remote)"
check "  ...and one for mixim-ai" test -f "$V/mixim-ai/vault-alice-personal/vault.toml"
check "  ...neither on GitHub" jq -e '.repos | has("alice/vault-alice-personal") or has("mixim-ai/vault-alice-personal") | not' "$GH"
check "mixim-ai's vaults are cloned" test -d "$V/mixim-ai/vault-public/.git" -a -d "$V/mixim-ai/vault-private/.git" -a -d "$V/mixim-ai/vault-hq/.git"
check "  ...but not a repo without vault.toml" test ! -e "$V/mixim-ai/vault-notes"
check "hooks installed" jq -e '[.hooks.SessionStart, .hooks.UserPromptSubmit, .hooks.PreToolUse] | map(.[0].hooks[0].command | endswith("vl hook")) | all' "$ROOT/alice/.claude/settings.json"
check "one basic-memory server, at user level" test "$(jq -r '.mcpServers | keys | join(",")' "$ROOT/alice/.claude/.claude.json")" = "basic-memory"
check "Basic Memory knows each vault by its short name" jq -e '.plugins["basic-memory"].data.projects | has("mixim-ai-public") and has("alice-personal") and has("mixim-ai-hq")' <(runtime alice)
check "the plugin writes to your personal vault by default" jq -e '.basicMemory.primaryProject == "alice-personal"' "$ROOT/alice/.claude/settings.json"
check "runtime.json knows notes_from" jq -e '.owners["mixim-ai"].notes_from["mixim-ai/marketing"] == "mixim-ai-public"' <(runtime alice)
check "  ...and the conflict" jq -e '.owners["mixim-ai"].conflicts["mixim-ai/both"] == ["mixim-ai-private", "mixim-ai-public"]' <(runtime alice)
check "status shows the conflict" grep -q "CONFLICT: mixim-ai/both" <<<"$(vl alice status)"

echo "== bob joins: GitHub decides what he gets"
vl bob org join mixim-ai >/dev/null
VB="$ROOT/bob/.vaultlines/vaults"
check "bob gets vault-public and vault-hq" test -d "$VB/mixim-ai/vault-public/.git" -a -d "$VB/mixim-ai/vault-hq/.git"
check "  ...but not vault-private" test ! -e "$VB/mixim-ai/vault-private"
check "  ...and his own personal vaults" test -f "$VB/mixim-ai/vault-bob-personal/vault.toml" -a -f "$VB/bob/vault-bob-personal/vault.toml"

echo "== code repos, cloned anywhere"
clone() { as "$1" git clone -q "file://$R/$2.git" "$ROOT/$1/$3" 2>/dev/null; echo "$ROOT/$1/$3"; }
A_MKT="$(clone alice mixim-ai/marketing code/marketing)"
B_MKT="$(clone bob mixim-ai/marketing work/elsewhere/mkt)"
A_JORGE="$(clone alice mixim-ai/jorge-ip-theft code/jorge)"
A_SHEETY="$(clone alice mixim-ai/sheety code/sheety)"
B_SHEETY="$(clone bob mixim-ai/sheety sheety)"
A_BOTH="$(clone alice mixim-ai/both code/both)"
A_BLOG="$(clone alice alice/blog code/blog)"
mkdir -p "$ROOT/alice/Desktop" "$ROOT/alice/code/marketing/src"
check "alice in marketing writes to vault-public" test "$(writes_of alice "$A_MKT" m1)" = mixim-ai-public
check "  ...from a subfolder too" test "$(writes_of alice "$A_MKT/src" m2)" = mixim-ai-public
check "bob's clone elsewhere writes there too" test "$(writes_of bob "$B_MKT" b1)" = mixim-ai-public
check "jorge-ip-theft writes to vault-private" test "$(writes_of alice "$A_JORGE" j1)" = mixim-ai-private
check "sheety (in no notes_from): alice's mixim-ai personal vault" test "$(writes_of alice "$A_SHEETY" s1)" = mixim-ai-alice-personal
check "  ...and bob's for bob" test "$(writes_of bob "$B_SHEETY" s2)" = mixim-ai-bob-personal
check "a repo in two notes_from: the personal vault" test "$(writes_of alice "$A_BOTH" c1)" = mixim-ai-alice-personal
check "  ...and the briefing says why" grep -q "is in notes_from of two vaults" <<<"$(start alice "$A_BOTH" c2)"
check "alice's own repo: her personal vault" test "$(writes_of alice "$A_BLOG" bl)" = alice-personal
check "no repo: her personal vault" test "$(writes_of alice "$ROOT/alice/Desktop" d1)" = alice-personal
check "marketing reads the other mixim-ai vaults" \
  test "$(session alice m1 | jq -c '.rules.reads')" = '["mixim-ai-alice-personal","mixim-ai-hq","mixim-ai-private"]'
check "the briefing names the vault and what it's about" \
  grep -q 'save notes from this repo to `mixim-ai-public` (Basic Memory project="mixim-ai-public"): Notes everyone at Mixim can see.' <<<"$(start alice "$A_MKT" m3)"
check "the Basic Memory block is in the repo" jq -e '.basicMemory.primaryProject == "mixim-ai-public"' "$A_MKT/.claude/settings.local.json"
check "  ...kept out of git" test -z "$(git -C "$A_MKT" status --porcelain)"

echo "== the hook guards what each session reads and writes"
check "the first session in a new clone asks before a shared write" \
  test "$(hook alice "$A_MKT" "$(write_event "$V/mixim-ai/vault-public/n.md" m1)")" = ask
check "  ...because Basic Memory may have briefed it from alice's personal vault" jq -e '.read | index("alice-personal")' <(session alice m1)
check "later sessions: writing vault-public is allowed" test "$(hook alice "$A_MKT" "$(write_event "$V/mixim-ai/vault-public/n.md" m3)")" = allow
hook alice "$A_MKT" "$(read_event "$V/mixim-ai/vault-private/plan.md" m3)" >/dev/null
check "  ...but after reading vault-private, it asks (bob can't see that)" \
  test "$(hook alice "$A_MKT" "$(write_event "$V/mixim-ai/vault-public/n.md" m3)")" = ask
check "the Desktop can't read mixim-ai vaults" test "$(hook alice "$ROOT/alice/Desktop" "$(read_event "$V/mixim-ai/vault-public/a.md" d1)")" = deny
check "mixim-ai repos can't read alice's own vaults" test "$(hook alice "$A_MKT" "$(read_event "$V/alice/vault-alice-personal/a.md" m2)")" = deny
check "vault-hq is read-only" test "$(hook alice "$A_MKT" "$(write_event "$V/mixim-ai/vault-hq/x.md" m2)")" = deny
check "vl's records can't be written" test "$(hook alice "$A_MKT" "$(write_event "$ROOT/alice/.vaultlines/state/runtime.json" m2)")" = deny
check "Google logins can't be read" test "$(hook alice "$A_MKT" "$(read_event "$ROOT/alice/.vaultlines/google/x.json" m2)")" = deny
hook alice "$A_MKT" '{"hook_event_name": "UserPromptSubmit", "session_id": "m2", "prompt": "tidy the notes"}' >/dev/null
check "Claude running vl org leave on its own asks" test "$(hook alice "$A_MKT" "$(bash_event "vl org leave mixim-ai" m2)")" = ask
hook alice "$A_MKT" '{"hook_event_name": "UserPromptSubmit", "session_id": "m2", "prompt": "use vl to leave"}' >/dev/null
check "  ...but not when you asked for vl" test "$(hook alice "$A_MKT" "$(bash_event "vl org leave mixim-ai" m2)")" = allow
check "vl sessions shows the session's vault" grep -q "mixim-ai-public" <<<"$(vl alice sessions)"
check "vl status previews where writes ask" grep -q "writes to mixim-ai-public ask after reading mixim-ai-private (bob can't see" <<<"$(vl alice sync --check-github >/dev/null; vl alice status)"

echo "== notes flow both ways"
bmtool bob write-note --title "From bob" --folder notes --content "- [fact] hello from bob" --project mixim-ai-public
bmtool bob write-note --title "Bob checkpoint" --folder sessions --content "- [x] private" --project mixim-ai-public
vl bob sync >/dev/null
vl alice sync >/dev/null
check "alice got bob's note" test -f "$V/mixim-ai/vault-public/notes/From bob.md"
check "bob's sessions/ stayed on bob's computer" test ! -e "$V/mixim-ai/vault-public/sessions/Bob checkpoint.md"
printf '\n- [fact] alice line\n' >> "$V/mixim-ai/vault-public/notes/From bob.md"
printf '\n- [fact] bob line\n' >> "$VB/mixim-ai/vault-public/notes/From bob.md"
vl alice sync >/dev/null && vl bob sync >/dev/null && vl alice sync >/dev/null
check "edits to the same note from both keep both lines" grep -q "bob line" "$V/mixim-ai/vault-public/notes/From bob.md"
check "  ...and alice's line too" grep -q "alice line" "$V/mixim-ai/vault-public/notes/From bob.md"

echo "== a vault.toml change reaches everyone on the next sync"
printf 'about      = "Notes everyone at Mixim can see."\nnotes_from = ["mixim-ai/marketing", "mixim-ai/mixim-workspace", "mixim-ai/sheety"]\n' \
  > "$ROOT/admin/mixim-ai/vault-public/vault.toml"
publish mixim-ai/vault-public "sheety too"
vl alice sync >/dev/null
check "sheety's notes go to vault-public now" test "$(writes_of alice "$A_SHEETY" s3)" = mixim-ai-public
check "  ...and the conflict is gone" jq -e '.owners["mixim-ai"].conflicts == {}' <(runtime alice)
check "  ...and sheety's Basic Memory block followed" jq -e '.basicMemory.primaryProject == "mixim-ai-public"' "$A_SHEETY/.claude/settings.local.json"

echo "== your changes in config.toml"
CONFIG="$ROOT/alice/.vaultlines/config.toml"
vl alice vault create alice/vault-recipes --about "Food." >/dev/null
check "a new vault stays on this computer" test -z "$(git -C "$V/alice/vault-recipes" remote)"
vl alice vault create mixim-ai/vault-founders --about "Founders." --notes_from mixim-ai/studio >/dev/null
A_STUDIO="$(clone alice mixim-ai/studio code/studio)"
check "  ...an org vault too, with its notes_from" test "$(writes_of alice "$A_STUDIO" st)" = mixim-ai-founders
check "  ...and nobody else gets it" jq -e '.repos | has("mixim-ai/vault-founders") | not' "$GH"
mkdir -p "$ROOT/alice/writing"
cat >> "$CONFIG" <<'EOF'

[repos."mixim-ai/jorge-ip-theft"]
writes = "mixim-ai/vault-public"
reads  = ["alice/vault-recipes"]

[repos."mixim-ai/*"]
reads = ["alice/vault-alice-personal"]

[folders."~/writing"]
writes = "alice/vault-recipes"
EOF
vl alice apply >/dev/null
check "a [repos] entry wins over notes_from" test "$(writes_of alice "$A_JORGE" j2)" = mixim-ai-public
check "  ...and adds its reads" jq -e '.rules.reads | index("alice-recipes")' <(session alice j2)
check "an owner-wide entry lets every mixim-ai repo read alice's own vault" \
  test "$(hook alice "$A_MKT" "$(read_event "$V/alice/vault-alice-personal/a.md" m4)")" = allow
check "a [folders] entry counts outside joined repos" test "$(writes_of alice "$ROOT/alice/writing" w1)" = alice-recipes

echo "== auto_pull keeps a repo up to date, wherever it's cloned"
A_WS="$(clone alice mixim-ai/mixim-workspace work/ws)"
printf '\n[repos."mixim-ai/mixim-workspace"]\nauto_pull = true\n' >> "$CONFIG"
vl alice apply >/dev/null
start alice "$A_WS" ws >/dev/null   # Claude ran there, so vl knows the clone
printf 'skill\n' > "$ROOT/admin/mixim-ai/mixim-workspace/skill.md"
publish mixim-ai/mixim-workspace "a skill"
vl alice sync >/dev/null
check "sync pulled the new commit" test -f "$A_WS/skill.md"
check "  ...and the plugin block stays out of that repo's git" test -z "$(git -C "$A_WS" status --porcelain)"

echo "== a vault from Google Drive"
if command -v rclone >/dev/null && command -v uv >/dev/null; then
  ACTION="$ROOT/action"
  admin clone -q "$R/mixim-ai/vault-hq.git" "$ACTION"
  (cd "$ACTION" && GITHUB_REPOSITORY=mixim-ai/vault-hq vl alice source refresh --here >/dev/null)
  check "the Action wrote notes and pushed them" test "$(git --git-dir "$R/mixim-ai/vault-hq.git" log -1 --format=%s)" = "Update from Google Drive"
  N="$(git --git-dir "$R/mixim-ai/vault-hq.git" rev-list --count HEAD)"
  (cd "$ACTION" && GITHUB_REPOSITORY=mixim-ai/vault-hq vl alice source refresh --here >/dev/null)
  check "  ...and a second run pushes nothing" test "$(git --git-dir "$R/mixim-ai/vault-hq.git" rev-list --count HEAD)" = "$N"
  vl alice sync >/dev/null && vl bob sync >/dev/null
  HQ="$VB/mixim-ai/vault-hq"
  check "bob got the notes" grep -q "Comptroller" "$HQ/Finance/Runway.xlsx.md"
  check "  ...pointing to the original" grep -qx 'fetch: "vl source fetch mixim-ai/vault-hq \\"Finance/Runway.xlsx\\""' "$HQ/Finance/Runway.xlsx.md"
  check "  ...and a note without text for what can't be converted" grep -qx 'text: "not convertible"' "$HQ/old.zip.md"
  echo "edited" >> "$HQ/old.zip.md"
  check "bob's sync takes the vault as GitHub has it" grep -q "local changes were moved to the branch" <<<"$(vl bob sync)"
  check "  ...keeping his change on a branch" test -n "$(git -C "$HQ" branch --list 'local-changes-*')"
  check "status shows when Drive last refreshed it" grep -q "from Google Drive, refreshed" <<<"$(vl bob status)"
else
  echo "  skip  rclone or uv isn't installed"
  vl alice sync >/dev/null && vl bob sync >/dev/null
fi
# The Action keeps Drive's file IDs; a local folder has none, so add two notes as it would.
note() { printf -- '---\ntitle: "%s"\ntype: "drive-file"\nsource: "gdrive"\nid: "%s"\npath: "%s"\nfetch: "vl source fetch mixim-ai/vault-hq \\"%s\\""\n---\n\ntext\n' "$1" "$2" "$3" "$3"; }
mkdir -p "$ROOT/admin/mixim-ai/vault-hq/Real" && note Runway F1 "Real/Runway.xlsx" > "$ROOT/admin/mixim-ai/vault-hq/Real/Runway.xlsx.md"
note Secret F403 "Real/Secret.pdf" > "$ROOT/admin/mixim-ai/vault-hq/Real/Secret.pdf.md"
publish mixim-ai/vault-hq "Update from Google Drive"
vl bob sync >/dev/null
FETCHED="$(vl bob source fetch mixim-ai/vault-hq "Real/Runway.xlsx" 2>/dev/null)"
check "vl source fetch logs in and gets one original" test "$(cat "$FETCHED")" = "PK original"
check "  ...into the fetch folder, read-only" test "$FETCHED" = "$ROOT/bob/.vaultlines/cache/fetch/mixim-ai/vault-hq/Real/Runway.xlsx" -a ! -w "$FETCHED"
check "  ...keeping the login for vl only" test "$(stat -f %Lp "$ROOT/bob/.vaultlines/google/1234-abc.json")" = 600
check "  ...and says so when Drive won't share a file" grep -q "can.t open this file in Drive. Ask for access to" <<<"$(vl bob source fetch mixim-ai/vault-hq "Real/Secret.pdf" 2>&1 || true)"
start bob "$B_MKT" f1 >/dev/null
check "the hook lets a session fetch from a vault it reads" test "$(hook bob "$B_MKT" "$(bash_event 'vl source fetch mixim-ai/vault-hq "Real/Runway.xlsx"' f1)")" = allow
check "  ...counting it as a read" jq -e '.read | index("mixim-ai-hq")' <(session bob f1)
check "  ...so a write to vault-public asks (bob can't list who reads vault-hq)" \
  test "$(hook bob "$B_MKT" "$(write_event "$VB/mixim-ai/vault-public/x.md" f1)")" = ask
check "reading a fetched original is a read" test "$(hook bob "$B_MKT" "$(read_event "$FETCHED" f1)")" = allow
check "  ...and writing it is denied" test "$(hook bob "$B_MKT" "$(write_event "$FETCHED" f1)")" = deny
check "the Desktop can't fetch" test "$(hook bob "$ROOT/bob" "$(bash_event 'vl source fetch mixim-ai/vault-hq x' d2)")" = deny

echo "== publishing a personal vault"
vl bob vault publish mixim-ai/vault-bob-personal >/dev/null
check "it's a private repo only bob can access" jq -e '.repos["mixim-ai/vault-bob-personal"] == {"push": ["bob"]}' "$GH"
check "  ...and bob's clone syncs to it" test -n "$(git -C "$VB/mixim-ai/vault-bob-personal" remote get-url origin)"

echo "== GitHub changes are caught by the daily check"
jq '.repos["mixim-ai/vault-public"].push += ["dan"] | .repos["mixim-ai/vault-private"].push = ["carol"]' "$GH" > "$GH.tmp" && mv "$GH.tmp" "$GH"
STATE="$ROOT/alice/.vaultlines/state/state.json"
vl alice sync --background >/dev/null 2>&1
check "no re-check within check_interval" jq -e '.vaults["mixim-ai-public"].audience.logins | index("dan") | not' <(runtime alice)
jq '.checked_at = 0' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"   # a day passes
OUT="$(vl alice sync --background 2>&1)"
check "the daily check updates who can see each vault" jq -e '.vaults["mixim-ai-public"].audience.logins | index("dan")' <(runtime alice)
check "  ...and stops syncing a vault alice lost" grep -q "mixim-ai/vault-private: no access on GitHub any more" <<<"$OUT"
check "  ...keeping its files" test -f "$V/mixim-ai/vault-private/vault.toml"
check "  ...and status says so" grep -q "no access on GitHub any more" <<<"$(vl alice status)"
start alice "$A_MKT" dan >/dev/null
hook alice "$A_MKT" "$(read_event "$V/mixim-ai/vault-alice-personal/a.md" dan)" >/dev/null
OUT="$(write_event "$V/mixim-ai/vault-public/c.md" dan | jq -c --arg cwd "$A_MKT" '. + {cwd: $cwd}' | CLAUDE_PROJECT_DIR="$A_MKT" vl alice hook)"
check "writing vault-public after a private read names dan" jq -e '.hookSpecificOutput.permissionDecisionReason | contains("dan")' <<<"$OUT"

echo "== old sessions are cleaned up"
SESSIONS="$ROOT/alice/.vaultlines/state/sessions"
touch -t 202001010000 "$SESSIONS/m1.json"
vl alice sync >/dev/null
check "sync deletes session files older than 30 days" test ! -e "$SESSIONS/m1.json"
check "  ...and keeps new ones" test -e "$SESSIONS/dan.json"

echo "== leave, doctor, uninstall"
vl bob org leave mixim-ai >/dev/null
check "leaving keeps the files" test -f "$ROOT/bob/.vaultlines/left/mixim-ai/vault-public/notes/From bob.md"
check "  ...and stops using them" jq -e '.owners | has("mixim-ai") | not' <(runtime bob)
check "doctor passes for alice" vl alice doctor
vl alice uninstall >/dev/null
check "uninstall removed the hooks" jq -e '.hooks == null' "$ROOT/alice/.claude/settings.json"
refuses "  ...so doctor fails" vl alice doctor

echo "All end-to-end checks passed."
