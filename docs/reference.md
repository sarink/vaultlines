# vl reference

Tables of keys, files and rules. The [README](../README.md) explains how they fit together.

## vault.toml

| Key | What it is |
|---|---|
| `about` | One line about the vault. Claude sees it at the start of a session. |
| `notes_from` | Repos (`OWNER/REPO`) whose sessions save notes here. Only repos of the vault's own owner count. A vault with a `[source]` can't have any. |
| `[source]` | The vault's notes come from a source, and it is read-only in sessions. `kind` names the source kind; the other keys belong to the kind. |

Problems in a `vault.toml` never stop `vl`: they show in `vl apply` and `vl doctor`.

## [source] keys

A source kind is a module in `plugins/`; its API is at the top of `plugins/__init__.py`. `vl vault create VAULT --source KIND --KEY VALUE ...` writes the keys.

### Kind `gdrive`

| Key | What it is |
|---|---|
| `folder_id` | The ID of a Drive folder, or of a whole shared drive. `--folder_id` also takes the folder's URL. On each refresh, vl asks Google which it is. |
| `max_size` | Bigger files get a note without text. Default: `"50M"`. |
| `client_id` | The client ID of a Google OAuth app of type "Desktop". |
| `client_secret` | Its secret. Google doesn't treat a desktop app's secret as secret. |

## config.toml

| Key | Default | What it is |
|---|---|---|
| `sync_interval` | `600` | Seconds between background syncs. |
| `check_interval` | `86400` | Seconds between checks with GitHub: new vaults, lost access, who can see each vault. |
| `on_leak` | `"ask"` | `"block"` refuses a write that would show notes to new people, instead of asking. |
| `basic_memory` | `true` | Set up Basic Memory. |
| `[repos."OWNER/REPO"]` | | `writes` (a vault ID), `reads` (added to the owner's vaults), `auto_pull`, `dangerously_skip_hook_guards` (dev mode: the hook allows every call in sessions here, and the briefing says so; running sessions follow it after `vl apply`). |
| `[repos."OWNER/*"]` | | `writes` and `reads` for every repo of the owner. A `[repos."OWNER/REPO"]` entry wins. |
| `[folders."PATH"]` | | `writes` and `reads`, for folders outside repos of owners you joined. The closest entry counts. |

Vaults are written as vault IDs, `OWNER/REPO`. An entry that names a vault that isn't on this computer is left out, with a warning. A vault with a source can't be `writes`.

## runtime.json (version 5)

What the hook reads, in `~/.vaultlines/state/runtime.json`. `vl apply` and the daily check write it. Vaults are keyed by vault ID.

```json
{"version": 5, "me": "kabir", "on_leak": "ask",
 "vaults": {"acme/vault-public": {"paths": ["…/vaults/acme/vault-public"], "show": "~/.vaultlines/vaults/acme/vault-public",
                                  "about": "…", "audience": {"kind": "people", "logins": ["kabir", "lee"]}},
            "acme/vault-hq": {"paths": ["…/vaults/acme/vault-hq"], "source": "gdrive",
                              "fetch": ["…/cache/fetch/acme/vault-hq"]}},
 "owners": {"acme": {"personal": "acme/vault-kabir-personal", "vaults": ["acme/vault-hq", "…"],
                     "notes_from": {"acme/marketing": "acme/vault-public"},
                     "conflicts": {"acme/both": ["acme/vault-private", "acme/vault-public"]}}},
 "repos": {"acme/website": {"writes": "acme/vault-public", "reads": []}},
 "folders": {"/Users/kabir/Documents/writing": {"writes": "kabir/vault-recipes", "reads": []}},
 "default": {"writes": "kabir/vault-kabir-personal", "reads": []},
 "plugins": {"basic-memory": {"kind": "basic-memory", "tool_prefixes": ["mcp__basic-memory__"],
                              "data": {"plugin": true, "projects": {"acme/vault-public": "acme/vault-public"}}}}}
```

## The hook's rules

Claude Code runs `vl hook` at SessionStart, on every prompt, and before Read, Write, Edit, MultiEdit, NotebookEdit, Grep, Glob, Bash and Basic Memory tool calls. It never reads TOML or asks GitHub: it reads `runtime.json`, `.git/config` files and its session records.

| When | Rule |
|---|---|
| SessionStart | Works out the session's rules (see the README) and keeps them in `state/sessions/ID.json`. A resumed session keeps its rules. A forked session, or one vl has no record of, starts as "only you". |
| SessionStart | Points the Basic Memory plugin at the session's vault, in `<repo>/.claude/settings.local.json` (excluded from git). The first session in a new clone counts the vault Basic Memory may have briefed it from before that (your personal vault) as read. |
| Every call | **Focus:** a vault that isn't the session's `writes` or `reads` is blocked. |
| Every read | **Label:** narrows the session label to the people who can see that vault. An unknown audience counts as "only you". |
| Every write | Asks (or, with `on_leak = "block"`, is blocked) when people who can see the vault couldn't see everything the session read. A write to a `reads` vault always asks. |
| Vault with a source | Read-only, and so is its fetch folder. Bash that mentions it counts as a read. `vl source fetch VAULT` in Bash is a read of that vault. |
| Basic Memory | A call without `project` gets one: the vault its `memory://` link starts with, or else the session's `writes` vault. `project_id`, `workspace`, searches of every project, and unknown arguments are blocked. |
| vl's own files | Writes to `~/.vaultlines/state` are blocked, and so is any access to `~/.vaultlines/google`. Edits to `config.toml` or vl's hooks, the commands `vl init`, `apply`, `uninstall`, `join`, `leave`, `vault`, `source login` and `source refresh`, and `VL_*` variables ask, unless your latest message mentions vl. |

## Environment variables

Tests run with `uv run pytest -q` and `tests/e2e.sh`, and set the test-only variables.

| Variable | What it is |
|---|---|
| `VL_HOME` | Where vl keeps everything, in place of `~/.vaultlines`. |
| `VL_SOURCE_TOKEN` | The login `vl source refresh` uses, if set: the refresh job's Actions secret. |
| `VL_FAKE_GITHUB` | Tests: a JSON file and bare repos, in place of GitHub. |
| `VL_FAKE_LOGIN` | Tests: who you are on that fake GitHub. |
| `VL_FAKE_GOOGLE` | Tests: the address of a fake Google, on 127.0.0.1. |
| `VL_TEST_REMOTES` | Tests: `1` counts `file://` remotes as GitHub, and a local folder as a `folder_id`. |
| `VL_NO_LAUNCHD` | Tests: `1` turns off the background sync. |
| `VL_NO_NOTIFY` | Tests: `1` turns off notifications. |

## Known problems

| Problem | What happens |
|---|---|
| GitHub Actions minutes | Each hourly refresh takes a minute or two. |
| A bot account costs a seat | With a company Gmail, the refresh job's bot account is a Google Workspace user, which is a paid seat. |
