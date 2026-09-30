#!/bin/bash
# End-to-end test on two fake computers ("alice" and "bob") with local git remotes.
# Touches nothing outside a temporary folder. Needs git, uv and claude.
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

# Run vl (or any command) as one of the fake computers.
as() {
  local who="$1"; shift
  HOME="$ROOT/$who" CLAUDE_CONFIG_DIR="$ROOT/$who/.claude" VAULTLINES_NO_LAUNCHD=1 \
    UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}" HF_HOME="${HF_HOME:-$REAL_HOME/.cache/huggingface}" \
    GIT_CONFIG_GLOBAL="$ROOT/$who/.gitconfig" "$@"
}
vl() { local who="$1"; shift; as "$who" uv run --quiet --project "$REPO" vl "$@"; }
bmtool() {
  local who="$1" out; shift
  out="$(as "$who" uvx basic-memory tool "$@" 2>&1)" || { echo "$out" | tail -5; return 1; }
}
servers() { jq -r --arg f "$2" '.projects[$f].mcpServers // {} | keys | join(",")' "$ROOT/$1/.claude/.claude.json"; }

mkdir -p "$ROOT/remotes" "$ROOT/alice" "$ROOT/bob"
for r in acme-private acme-public; do git init -q --bare -b main "$ROOT/remotes/$r.git"; done
R="file://$ROOT/remotes"

echo "== alice: init, two team vaults, two bound folders"
vl alice init --personal-remote none >/dev/null
vl alice vault create acme-private --level private --team acme --remote "$R/acme-private.git" >/dev/null
vl alice vault create acme-public --level public --team acme --remote "$R/acme-public.git" >/dev/null
LEGAL="$(cd "$ROOT/alice" && mkdir -p work/legal && cd work/legal && git init -q && pwd -P)"
APP="$(cd "$ROOT/alice" && mkdir -p work/app && cd work/app && git init -q && pwd -P)"
vl alice bind acme-private "$LEGAL" >/dev/null
vl alice bind acme-public "$APP" >/dev/null

check "personal vault is available everywhere" \
  test "$(jq -r '.mcpServers | keys | join(",")' "$ROOT/alice/.claude/.claude.json")" = "vl-personal"
check "legal folder: private + public servers" test "$(servers alice "$LEGAL")" = "vl-acme-private,vl-acme-public"
check "app folder: public server only" test "$(servers alice "$APP")" = "vl-acme-public"
check "legal folder blocks personal" jq -e '.permissions.deny == ["mcp__vl-personal"]' "$LEGAL/.claude/settings.local.json"
check "legal folder asks before writing public" jq -e '.permissions.ask | index("mcp__vl-acme-public__write_note")' "$LEGAL/.claude/settings.local.json"
check "legal folder writes to acme-private" jq -e '.basicMemory.primaryProject == "acme-private"' "$LEGAL/.claude/settings.local.json"
check "admin tools denied at user level" jq -e '.permissions.deny | index("mcp__vl-acme-private__delete_project")' "$ROOT/alice/.claude/settings.json"
check "settings.local.json kept out of git" test -z "$(git -C "$LEGAL" status --porcelain)"

BEFORE="$(jq -S '{m: .mcpServers, p: (.projects | map_values(.mcpServers))}' "$ROOT/alice/.claude/.claude.json")"
vl alice apply >/dev/null
AFTER="$(jq -S '{m: .mcpServers, p: (.projects | map_values(.mcpServers))}' "$ROOT/alice/.claude/.claude.json")"
check "apply twice changes nothing" test "$BEFORE" = "$AFTER"

echo "== bob: joins the public vault; notes flow both ways"
vl bob init --personal-remote none >/dev/null
vl bob vault join "$R/acme-public.git" --level public --team acme >/dev/null
bmtool bob write-note --title "From bob" --folder notes --content "- [fact] hello from bob" --project acme-public
bmtool bob write-note --title "Bob checkpoint" --folder sessions --content "- [x] private" --project acme-public
vl bob sync >/dev/null
vl alice sync >/dev/null
check "alice got bob's note" test -f "$ROOT/alice/Vaults/acme-public/notes/From bob.md"
check "bob's sessions/ stayed on bob's computer" test ! -e "$ROOT/alice/Vaults/acme-public/sessions/Bob checkpoint.md"

printf '\n- [fact] alice line\n' >> "$ROOT/alice/Vaults/acme-public/notes/From bob.md"
printf '\n- [fact] bob line\n' >> "$ROOT/bob/Vaults/acme-public/notes/From bob.md"
vl alice sync >/dev/null && vl bob sync >/dev/null && vl alice sync >/dev/null
check "edits to the same note from both keep both lines" \
  grep -q "bob line" "$ROOT/alice/Vaults/acme-public/notes/From bob.md"
check "  ...and alice's line too" grep -q "alice line" "$ROOT/alice/Vaults/acme-public/notes/From bob.md"
check "bob never sees the private vault" test ! -e "$ROOT/bob/Vaults/acme-private"

echo "== adopt, unbind, remove"
mkdir -p "$ROOT/alice/old-notes" && echo "# Old" > "$ROOT/alice/old-notes/old.md"
vl alice vault adopt "$ROOT/alice/old-notes" --level personal >/dev/null
check "adopted folder became a git repo" test -d "$ROOT/alice/old-notes/.git"
vl alice unbind "$APP" >/dev/null
check "unbind removed the folder's servers" test -z "$(servers alice "$APP")"
check "unbind removed the folder's vault settings" jq -e '.basicMemory == null' "$APP/.claude/settings.local.json"
vl bob vault remove acme-public >/dev/null
check "remove kept the files" test -f "$ROOT/bob/Vaults/acme-public/notes/From bob.md"
check "doctor passes for alice" vl alice doctor

echo "All end-to-end checks passed."
