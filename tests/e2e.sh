#!/bin/bash
# End-to-end test on two fake computers, "alice" and "bob", in the fake GitHub org acme.
# A JSON file and bare repos stand in for GitHub, a local server for Google, and a local
# folder for the Drive folder. Touches nothing outside a temporary folder. Needs git, jq,
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
  HOME="$ROOT/$who" VL_HOME="$ROOT/$who/.vaultlines" CLAUDE_CONFIG_DIR="$ROOT/$who/.claude" \
    VL_NO_LAUNCHD=1 VL_NO_NOTIFY=1 VL_TEST_REMOTES=1 \
    VL_FAKE_GITHUB="$GH" VL_FAKE_LOGIN="$who" VL_FAKE_GOOGLE="$GOOGLE" \
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
jq -n --arg r "$R" '{root: $r, orgs: {"acme": ["alice", "bob"]}, repos: {}}' > "$GH"
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
repo acme/vault-public "$BOTH" vault.toml 'about      = "Notes everyone at Acme can see."
notes_from = ["acme/marketing", "acme/acme-workspace", "acme/both"]
'
repo acme/vault-private '{"push": ["alice"]}' vault.toml 'about      = "Founders notes: fundraising, hiring, legal."
notes_from = ["acme/legal-case", "acme/both"]
'
DRIVE="$ROOT/drive"
mkdir -p "$DRIVE/Team Docs" "$DRIVE/Finance"
echo "# Plan" > "$DRIVE/Team Docs/Plan.md"
cp "$REPO/tests/fixtures/sample.xlsx" "$DRIVE/Finance/Runway.xlsx"
printf 'PK\005\006' > "$DRIVE/old.zip"
repo acme/vault-hq '{"push": ["alice"], "read": ["bob"]}' vault.toml "about = \"The text of every file in Acme HQ, in Google Drive. Claude only reads it.\"

[source]
kind          = \"gdrive\"
folder_id     = \"$DRIVE\"
max_size      = \"50M\"
client_id     = \"1234-abc.apps.googleusercontent.com\"
client_secret = \"GOCSPX-x\"
"
repo acme/vault-notes "$BOTH" README.md 'no vault.toml, so not a vault'
for code in marketing billing acme-workspace both studio; do repo "acme/$code" "$BOTH" README.md "# $code"; done
repo acme/legal-case '{"push": ["alice"]}' README.md '# case'
repo alice/blog '{"push": ["alice"]}' README.md '# blog'

# ---------------------------------------------------------------- a fake Google
GOOGLE_FILES="$ROOT/google-files.json"
jq -n '{F1: {name: "Runway.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data: "PK original"},
        F403: {name: "Secret.pdf", mimeType: "application/pdf", status: 403}}' > "$GOOGLE_FILES"
uv run --quiet --project "$REPO" python "$REPO/tests/fake_google.py" "$GOOGLE_FILES" > "$ROOT/google.url" &
GOOGLE_PID=$!
for _ in $(seq 50); do [ -s "$ROOT/google.url" ] && break; sleep 0.1; done
GOOGLE="$(cat "$ROOT/google.url")"

echo "== alice joins acme: one command sets everything up"
vl alice org join acme >/dev/null
V="$ROOT/alice/.vaultlines/vaults"
check "vl init ran first: config.toml is comments only" test -z "$(grep -v '^#' "$ROOT/alice/.vaultlines/config.toml" | grep -v '^$' || true)"
check "your personal vault, on this computer only" test -z "$(git -C "$V/alice/vault-alice-personal" remote)"
check "  ...and one for acme" test -f "$V/acme/vault-alice-personal/vault.toml"
check "  ...neither on GitHub" jq -e '.repos | has("alice/vault-alice-personal") or has("acme/vault-alice-personal") | not' "$GH"
check "acme's vaults are cloned" test -d "$V/acme/vault-public/.git" -a -d "$V/acme/vault-private/.git" -a -d "$V/acme/vault-hq/.git"
check "  ...but not a repo without vault.toml" test ! -e "$V/acme/vault-notes"
check "hooks installed" jq -e '[.hooks.SessionStart, .hooks.UserPromptSubmit, .hooks.PreToolUse] | map(.[0].hooks[0].command | endswith("vl hook")) | all' "$ROOT/alice/.claude/settings.json"
check "one basic-memory server, at user level" test "$(jq -r '.mcpServers | keys | join(",")' "$ROOT/alice/.claude/.claude.json")" = "basic-memory"
check "Basic Memory knows each vault by its ID" jq -e '.plugins["basic-memory"].data.projects | has("acme/vault-public") and has("alice/vault-alice-personal") and has("acme/vault-hq")' <(runtime alice)
check "the plugin writes to your personal vault by default" jq -e '.basicMemory.primaryProject == "alice/vault-alice-personal"' "$ROOT/alice/.claude/settings.json"
check "runtime.json knows notes_from" jq -e '.owners["acme"].notes_from["acme/marketing"] == "acme/vault-public"' <(runtime alice)
check "  ...and the conflict" jq -e '.owners["acme"].conflicts["acme/both"] == ["acme/vault-private", "acme/vault-public"]' <(runtime alice)
check "status shows the conflict" grep -q "CONFLICT: acme/both" <<<"$(vl alice status)"

echo "== bob joins: GitHub decides what he gets"
vl bob org join acme >/dev/null
VB="$ROOT/bob/.vaultlines/vaults"
check "bob gets vault-public and vault-hq" test -d "$VB/acme/vault-public/.git" -a -d "$VB/acme/vault-hq/.git"
check "  ...but not vault-private" test ! -e "$VB/acme/vault-private"
check "  ...and his own personal vaults" test -f "$VB/acme/vault-bob-personal/vault.toml" -a -f "$VB/bob/vault-bob-personal/vault.toml"

echo "== code repos, cloned anywhere"
clone() { as "$1" git clone -q "file://$R/$2.git" "$ROOT/$1/$3" 2>/dev/null; echo "$ROOT/$1/$3"; }
A_MKT="$(clone alice acme/marketing code/marketing)"
B_MKT="$(clone bob acme/marketing work/elsewhere/mkt)"
A_LEGAL="$(clone alice acme/legal-case code/legal)"
A_BILLING="$(clone alice acme/billing code/billing)"
B_BILLING="$(clone bob acme/billing billing)"
A_BOTH="$(clone alice acme/both code/both)"
A_BLOG="$(clone alice alice/blog code/blog)"
mkdir -p "$ROOT/alice/Desktop" "$ROOT/alice/code/marketing/src"
check "alice in marketing writes to vault-public" test "$(writes_of alice "$A_MKT" m1)" = acme/vault-public
check "  ...from a subfolder too" test "$(writes_of alice "$A_MKT/src" m2)" = acme/vault-public
check "bob's clone elsewhere writes there too" test "$(writes_of bob "$B_MKT" b1)" = acme/vault-public
check "legal-case writes to vault-private" test "$(writes_of alice "$A_LEGAL" j1)" = acme/vault-private
check "billing (in no notes_from): alice's acme personal vault" test "$(writes_of alice "$A_BILLING" s1)" = acme/vault-alice-personal
check "  ...and bob's for bob" test "$(writes_of bob "$B_BILLING" s2)" = acme/vault-bob-personal
check "a repo in two notes_from: the personal vault" test "$(writes_of alice "$A_BOTH" c1)" = acme/vault-alice-personal
check "  ...and the briefing says why" grep -q "is in notes_from of two vaults" <<<"$(start alice "$A_BOTH" c2)"
check "alice's own repo: her personal vault" test "$(writes_of alice "$A_BLOG" bl)" = alice/vault-alice-personal
check "no repo: her personal vault" test "$(writes_of alice "$ROOT/alice/Desktop" d1)" = alice/vault-alice-personal
check "marketing reads the other acme vaults" \
  test "$(session alice m1 | jq -c '.rules.reads')" = '["acme/vault-alice-personal","acme/vault-hq","acme/vault-private"]'
check "the briefing names the vault and what it's about" \
  grep -q 'save notes from this repo to `acme/vault-public` (Basic Memory project="acme/vault-public"): Notes everyone at Acme can see.' <<<"$(start alice "$A_MKT" m3)"
check "the Basic Memory block is in the repo" jq -e '.basicMemory.primaryProject == "acme/vault-public"' "$A_MKT/.claude/settings.local.json"
check "  ...kept out of git" test -z "$(git -C "$A_MKT" status --porcelain)"

echo "== the hook guards what each session reads and writes"
check "the first session in a new clone asks before a shared write" \
  test "$(hook alice "$A_MKT" "$(write_event "$V/acme/vault-public/n.md" m1)")" = ask
check "  ...because Basic Memory may have briefed it from alice's personal vault" jq -e '.read | index("alice/vault-alice-personal")' <(session alice m1)
check "later sessions: writing vault-public is allowed" test "$(hook alice "$A_MKT" "$(write_event "$V/acme/vault-public/n.md" m3)")" = allow
hook alice "$A_MKT" "$(read_event "$V/acme/vault-private/plan.md" m3)" >/dev/null
check "  ...but after reading vault-private, it asks (bob can't see that)" \
  test "$(hook alice "$A_MKT" "$(write_event "$V/acme/vault-public/n.md" m3)")" = ask
check "the Desktop can't read acme vaults" test "$(hook alice "$ROOT/alice/Desktop" "$(read_event "$V/acme/vault-public/a.md" d1)")" = deny
check "acme repos can't read alice's own vaults" test "$(hook alice "$A_MKT" "$(read_event "$V/alice/vault-alice-personal/a.md" m2)")" = deny
check "vault-hq is read-only" test "$(hook alice "$A_MKT" "$(write_event "$V/acme/vault-hq/x.md" m2)")" = deny
check "vl's records can't be written" test "$(hook alice "$A_MKT" "$(write_event "$ROOT/alice/.vaultlines/state/runtime.json" m2)")" = deny
check "Google logins can't be read" test "$(hook alice "$A_MKT" "$(read_event "$ROOT/alice/.vaultlines/google/x.json" m2)")" = deny
hook alice "$A_MKT" '{"hook_event_name": "UserPromptSubmit", "session_id": "m2", "prompt": "tidy the notes"}' >/dev/null
check "Claude running vl org leave on its own asks" test "$(hook alice "$A_MKT" "$(bash_event "vl org leave acme" m2)")" = ask
hook alice "$A_MKT" '{"hook_event_name": "UserPromptSubmit", "session_id": "m2", "prompt": "use vl to leave"}' >/dev/null
check "  ...but not when you asked for vl" test "$(hook alice "$A_MKT" "$(bash_event "vl org leave acme" m2)")" = allow
check "vl sessions shows the session's vault" grep -q "acme/vault-public" <<<"$(vl alice sessions)"
check "vl status previews where writes ask" grep -q "writes to acme/vault-public ask after reading acme/vault-private (bob can't see" <<<"$(vl alice sync --check-github >/dev/null; vl alice status)"

echo "== notes flow both ways"
bmtool bob write-note --title "From bob" --folder notes --content "- [fact] hello from bob" --project acme/vault-public
bmtool bob write-note --title "Bob checkpoint" --folder sessions --content "- [x] private" --project acme/vault-public
vl bob sync >/dev/null
vl alice sync >/dev/null
check "alice got bob's note" test -f "$V/acme/vault-public/notes/From bob.md"
check "bob's sessions/ stayed on bob's computer" test ! -e "$V/acme/vault-public/sessions/Bob checkpoint.md"
printf '\n- [fact] alice line\n' >> "$V/acme/vault-public/notes/From bob.md"
printf '\n- [fact] bob line\n' >> "$VB/acme/vault-public/notes/From bob.md"
vl alice sync >/dev/null && vl bob sync >/dev/null && vl alice sync >/dev/null
check "edits to the same note from both keep both lines" grep -q "bob line" "$V/acme/vault-public/notes/From bob.md"
check "  ...and alice's line too" grep -q "alice line" "$V/acme/vault-public/notes/From bob.md"

echo "== a vault.toml change reaches everyone on the next sync"
printf 'about      = "Notes everyone at Acme can see."\nnotes_from = ["acme/marketing", "acme/acme-workspace", "acme/billing"]\n' \
  > "$ROOT/admin/acme/vault-public/vault.toml"
publish acme/vault-public "billing too"
vl alice sync >/dev/null
check "billing's notes go to vault-public now" test "$(writes_of alice "$A_BILLING" s3)" = acme/vault-public
check "  ...and the conflict is gone" jq -e '.owners["acme"].conflicts == {}' <(runtime alice)
check "  ...and billing's Basic Memory block followed" jq -e '.basicMemory.primaryProject == "acme/vault-public"' "$A_BILLING/.claude/settings.local.json"

echo "== your changes in config.toml"
CONFIG="$ROOT/alice/.vaultlines/config.toml"
vl alice vault create alice/vault-recipes --about "Food." >/dev/null
check "a new vault stays on this computer" test -z "$(git -C "$V/alice/vault-recipes" remote)"
vl alice vault create acme/vault-founders --about "Founders." --notes_from acme/studio >/dev/null
A_STUDIO="$(clone alice acme/studio code/studio)"
check "  ...an org vault too, with its notes_from" test "$(writes_of alice "$A_STUDIO" st)" = acme/vault-founders
check "  ...and nobody else gets it" jq -e '.repos | has("acme/vault-founders") | not' "$GH"
mkdir -p "$ROOT/alice/writing"
cat >> "$CONFIG" <<'EOF'

[repos."acme/legal-case"]
writes = "acme/vault-public"
reads  = ["alice/vault-recipes"]

[repos."acme/*"]
reads = ["alice/vault-alice-personal"]

[folders."~/writing"]
writes = "alice/vault-recipes"
EOF
vl alice apply >/dev/null
check "a [repos] entry wins over notes_from" test "$(writes_of alice "$A_LEGAL" j2)" = acme/vault-public
check "  ...and adds its reads" jq -e '.rules.reads | index("alice/vault-recipes")' <(session alice j2)
check "an owner-wide entry lets every acme repo read alice's own vault" \
  test "$(hook alice "$A_MKT" "$(read_event "$V/alice/vault-alice-personal/a.md" m4)")" = allow
check "a [folders] entry counts outside joined repos" test "$(writes_of alice "$ROOT/alice/writing" w1)" = alice/vault-recipes

echo "== auto_pull keeps a repo up to date, wherever it's cloned"
A_WS="$(clone alice acme/acme-workspace work/ws)"
printf '\n[repos."acme/acme-workspace"]\nauto_pull = true\n' >> "$CONFIG"
vl alice apply >/dev/null
start alice "$A_WS" ws >/dev/null   # Claude ran there, so vl knows the clone
printf 'skill\n' > "$ROOT/admin/acme/acme-workspace/skill.md"
publish acme/acme-workspace "a skill"
vl alice sync >/dev/null
check "sync pulled the new commit" test -f "$A_WS/skill.md"
check "  ...and the plugin block stays out of that repo's git" test -z "$(git -C "$A_WS" status --porcelain)"

echo "== a vault from Google Drive"
if command -v rclone >/dev/null && command -v uv >/dev/null; then
  ACTION="$ROOT/action"
  admin clone -q "file://$R/acme/vault-hq.git" "$ACTION"   # its origin names the vault, like GitHub's checkout
  # The refresh job's two steps, in its checkout: fetch with the login, then convert without it.
  (cd "$ACTION" && vl alice source refresh --fetch-only >/dev/null && vl alice source refresh --convert-only >/dev/null)
  check "the refresh job wrote notes and pushed them" test "$(git --git-dir "$R/acme/vault-hq.git" log -1 --format=%s)" = "Update from Google Drive"
  N="$(git --git-dir "$R/acme/vault-hq.git" rev-list --count HEAD)"
  vl alice source refresh acme/vault-hq >/dev/null   # on alice's computer, in her clone
  check "  ...and a refresh on a laptop pushes nothing new" test "$(git --git-dir "$R/acme/vault-hq.git" rev-list --count HEAD)" = "$N"
  vl alice sync >/dev/null && vl bob sync >/dev/null
  HQ="$VB/acme/vault-hq"
  check "bob got the notes" grep -q "Comptroller" "$HQ/Finance/Runway.xlsx.md"
  check "  ...pointing to the original" grep -qx 'fetch: "vl source fetch acme/vault-hq \\"Finance/Runway.xlsx\\""' "$HQ/Finance/Runway.xlsx.md"
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
note() { printf -- '---\ntitle: "%s"\ntype: "drive-file"\nsource: "gdrive"\nid: "%s"\npath: "%s"\nfetch: "vl source fetch acme/vault-hq \\"%s\\""\n---\n\ntext\n' "$1" "$2" "$3" "$3"; }
mkdir -p "$ROOT/admin/acme/vault-hq/Real" && note Runway F1 "Real/Runway.xlsx" > "$ROOT/admin/acme/vault-hq/Real/Runway.xlsx.md"
note Secret F403 "Real/Secret.pdf" > "$ROOT/admin/acme/vault-hq/Real/Secret.pdf.md"
publish acme/vault-hq "Update from Google Drive"
vl bob sync >/dev/null
FETCHED="$(vl bob source fetch acme/vault-hq "Real/Runway.xlsx" 2>/dev/null)"
check "vl source fetch logs in and gets one original" test "$(cat "$FETCHED")" = "PK original"
check "  ...into the fetch folder, read-only" test "$FETCHED" = "$ROOT/bob/.vaultlines/cache/fetch/acme/vault-hq/Real/Runway.xlsx" -a ! -w "$FETCHED"
check "  ...keeping the login for vl only" test "$(stat -f %Lp "$ROOT/bob/.vaultlines/google/1234-abc.json")" = 600
check "  ...and says so when Drive won't share a file" grep -q "can.t open this file in Drive. Ask for access to" <<<"$(vl bob source fetch acme/vault-hq "Real/Secret.pdf" 2>&1 || true)"
start bob "$B_MKT" f1 >/dev/null
check "the hook lets a session fetch from a vault it reads" test "$(hook bob "$B_MKT" "$(bash_event 'vl source fetch acme/vault-hq "Real/Runway.xlsx"' f1)")" = allow
check "  ...counting it as a read" jq -e '.read | index("acme/vault-hq")' <(session bob f1)
check "  ...so a write to vault-public asks (bob can't list who reads vault-hq)" \
  test "$(hook bob "$B_MKT" "$(write_event "$VB/acme/vault-public/x.md" f1)")" = ask
check "reading a fetched original is a read" test "$(hook bob "$B_MKT" "$(read_event "$FETCHED" f1)")" = allow
check "  ...and writing it is denied" test "$(hook bob "$B_MKT" "$(write_event "$FETCHED" f1)")" = deny
check "the Desktop can't fetch" test "$(hook bob "$ROOT/bob" "$(bash_event 'vl source fetch acme/vault-hq x' d2)")" = deny

echo "== publishing a personal vault"
vl bob vault publish acme/vault-bob-personal >/dev/null
check "it's a private repo only bob can access" jq -e '.repos["acme/vault-bob-personal"] == {"push": ["bob"]}' "$GH"
check "  ...and bob's clone syncs to it" test -n "$(git -C "$VB/acme/vault-bob-personal" remote get-url origin)"

echo "== GitHub changes are caught by the daily check"
jq '.repos["acme/vault-public"].push += ["dan"] | .repos["acme/vault-private"].push = ["carol"]' "$GH" > "$GH.tmp" && mv "$GH.tmp" "$GH"
STATE="$ROOT/alice/.vaultlines/state/state.json"
vl alice sync --background >/dev/null 2>&1
check "no re-check within check_interval" jq -e '.vaults["acme/vault-public"].audience.logins | index("dan") | not' <(runtime alice)
jq '.checked_at = 0' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"   # a day passes
OUT="$(vl alice sync --background 2>&1)"
check "the daily check updates who can see each vault" jq -e '.vaults["acme/vault-public"].audience.logins | index("dan")' <(runtime alice)
check "  ...and stops syncing a vault alice lost" grep -q "acme/vault-private: no access on GitHub any more" <<<"$OUT"
check "  ...keeping its files" test -f "$V/acme/vault-private/vault.toml"
check "  ...and status says so" grep -q "no access on GitHub any more" <<<"$(vl alice status)"
start alice "$A_MKT" dan >/dev/null
hook alice "$A_MKT" "$(read_event "$V/acme/vault-alice-personal/a.md" dan)" >/dev/null
OUT="$(write_event "$V/acme/vault-public/c.md" dan | jq -c --arg cwd "$A_MKT" '. + {cwd: $cwd}' | CLAUDE_PROJECT_DIR="$A_MKT" vl alice hook)"
check "writing vault-public after a private read names dan" jq -e '.hookSpecificOutput.permissionDecisionReason | contains("dan")' <<<"$OUT"

echo "== old sessions are cleaned up"
SESSIONS="$ROOT/alice/.vaultlines/state/sessions"
touch -t 202001010000 "$SESSIONS/m1.json"
vl alice sync >/dev/null
check "sync deletes session files older than 30 days" test ! -e "$SESSIONS/m1.json"
check "  ...and keeps new ones" test -e "$SESSIONS/dan.json"

echo "== leave, doctor, uninstall"
vl bob org leave acme >/dev/null
check "leaving keeps the files" test -f "$ROOT/bob/.vaultlines/left/acme/vault-public/notes/From bob.md"
check "  ...and stops using them" jq -e '.owners | has("acme") | not' <(runtime bob)
check "doctor passes for alice" vl alice doctor
vl alice uninstall >/dev/null
check "uninstall removed the hooks" jq -e '.hooks == null' "$ROOT/alice/.claude/settings.json"
refuses "  ...so doctor fails" vl alice doctor

echo "All end-to-end checks passed."
