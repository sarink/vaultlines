# vl reference

Details the README leaves out. For how things fit together, read the README first.

## vault.toml

| Key | What it is |
|---|---|
| `about` | One line about the vault. Claude sees it at the start of a session. |
| `notes_from` | Repos (`OWNER/REPO`) whose sessions save notes here. Only repos of the vault's own owner count. A vault filled from a `[source]` can't have any. |
| `[source]` | The vault is filled from elsewhere, and is read-only in sessions. `kind` says from what. |

Problems in a `vault.toml` never stop `vl`: they show in `vl apply` and `vl doctor`.

### `[source]`: a vault filled from a source

`kind` names the source kind; the other keys belong to the kind. `vl vault create VAULT --source KIND --KEY VALUE ...` writes them: each flag is a key, spelled the same. A source kind is a module in `plugins/` (see the API at the top of `plugins/__init__.py`).

Every kind works the same way:

- The vault's **fill job** is `.github/workflows/vl-source.yml`. It runs `vl source refresh --here` every hour, and on demand (`vl source refresh VAULT [--force]`). It logs in to the source with the Actions secret `VL_SOURCE_TOKEN`, which `vl vault create` sets. Its commits are "Update from KIND NAME", like "Update from Google Drive".
- On your computer, `vl sync` only pulls the vault. Local changes are saved on a branch `local-changes-DATE`, and the vault is reset to GitHub's.
- In sessions, the vault (and its fetch folder) is read-only.

### Kind `gdrive`

| Key | What it is |
|---|---|
| `shared_drive` | The shared drive's name (or ID). The fill job looks up its ID on each refresh. |
| `folder` | Only this folder of the drive (default `""`: all of it). |
| `max_size` | Bigger files get a note without text (default `"50M"`). |
| `google_client_id`, `google_client_secret` | The Google OAuth app (desktop type). Its secret isn't secret: Google says so for desktop apps. |

`vl vault create --source gdrive` asks at a terminal for the keys left out: the client ID and secret first (after showing how to make them), then, once the bot account has logged in, the shared drive, picked from the bot's list. Without a terminal, it stops and prints the steps. `vl vault create --source gdrive --help` prints them too.

`VL_SOURCE_TOKEN` is the bot account's read-only refresh token. Each refresh checks with Google that it can only read, lists the drive, converts new and changed files with markitdown, and writes one note per file. Notes keep keys that others added to their frontmatter.

`vl source fetch VAULT PATH` finds the note whose frontmatter `path` is PATH and downloads the file by its Drive ID, so renames don't break it. Google's own files are exported: Docs to `.docx`, Sheets to `.xlsx`, Slides to `.pptx`, Drawings to `.pdf`. It uses your own read-only Google login (`vl source login VAULT`), kept in `~/.vaultlines/google/`.

## config.toml

| Key | Default | What it is |
|---|---|---|
| `sync_interval` | `600` | Seconds between background syncs. |
| `check_interval` | `86400` | Seconds between checks with GitHub: new vaults, lost access, who can see each vault. |
| `on_leak` | `"ask"` | `"block"` refuses a write that would show notes to new people, instead of asking. |
| `basic_memory` | `true` | Set up Basic Memory. |
| `[repos."OWNER/REPO"]` | | `writes` (a vault ID), `reads` (added to the owner's vaults), `auto_pull`, `allow_vl_commands` (sessions here run vl commands without asking). |
| `[repos."OWNER/*"]` | | `writes` and `reads` for every repo of the owner. A `[repos."OWNER/REPO"]` entry wins. |
| `[folders."PATH"]` | | `writes` and `reads`, for folders outside repos of owners you joined. The closest entry counts. |

Vaults are always written as IDs: `OWNER/vault-NAME`. An entry that names a vault that isn't on this computer is left out, with a warning. A vault with a source can't be `writes`.

## Commands and options

```
vl init [--publish]
vl org join ORG
vl org leave ORG [--delete-files]
vl vault create VAULT [--about TEXT] [--notes_from REPO]... [--publish]
vl vault create VAULT --source KIND [--KEY VALUE]... [--about TEXT]     # always published
vl vault publish VAULT
vl source refresh VAULT [--force]
vl source fetch VAULT PATH
vl source login VAULT
vl sync [VAULT] [--check-github]
vl status | doctor | apply | sessions [--limit N] | uninstall
```

A `VAULT` is `OWNER/vault-NAME` or its short name, like `mixim-ai-hq`. Flags that set a key of `vault.toml` are spelled like the key.

## Discovery

`vl org join` and the daily check with GitHub ask for the org's repos you can access (`orgs/OWNER/repos`, or `user/repos` for your own account), keep the names that start with `vault-`, and check each for `vault.toml`. New vaults are cloned. A vault you can't access any more stops syncing; its files stay, and sessions can't use it.

## The hook

Claude Code runs `vl hook` at SessionStart, on every prompt, and before Read, Write, Edit, MultiEdit, NotebookEdit, Grep, Glob, Bash and Basic Memory tool calls. It only reads `~/.vaultlines/state/runtime.json` and `.git/config` files; it never reads TOML or asks GitHub.

At SessionStart it works out the session's rules (see the README), keeps them in the session record (`state/sessions/ID.json`), and points the Basic Memory plugin at the session's vault in `<repo>/.claude/settings.local.json` (excluded from git). A resumed session keeps its rules. A forked session, or one `vl` has no record of, starts as "only you".

For every call:

1. **Focus:** a vault that isn't the session's `writes` or `reads` is blocked.
2. **Label:** each read narrows the label to the people who can see that vault. Unknown audiences count as "only you".
3. **Writes** ask (or are blocked, with `on_leak = "block"`) when people who can see the vault couldn't see everything the session read.
4. Writes to a `reads` vault always ask.
5. A vault with a source (and its fetch folder) is read-only. Bash that mentions it counts as a read.
6. `vl source fetch VAULT` in Bash is a read of that vault.
7. vl's own files: writes to `~/.vaultlines/state` are blocked; any access to `~/.vaultlines/google` is blocked; edits to `config.toml`, to vl's hooks, and the commands `vl init`, `apply`, `uninstall`, `org`, `vault`, `source login`, or `VAULTLINES_*` variables ask, unless your latest message mentions vl, or the repo's `[repos]` entry has `allow_vl_commands = true`.

The first session in a new clone counts the vault Basic Memory may have briefed it from before `vl` wrote the repo's block (your personal vault), so its first shared write may ask.

## runtime.json (version 4)

```json
{"version": 4, "me": "kabir", "on_leak": "ask",
 "vaults": {"mixim-ai-public": {"id": "mixim-ai/vault-public", "paths": ["…/vaults/mixim-ai/vault-public"],
                                "about": "…", "audience": {"kind": "people", "logins": ["kabir", "jorge"]}},
            "mixim-ai-hq": {"id": "mixim-ai/vault-hq", "source": "gdrive", "fetch": ["…/cache/fetch/mixim-ai/vault-hq"]}},
 "owners": {"mixim-ai": {"personal": "mixim-ai-kabir-personal", "vaults": ["mixim-ai-public", "…"],
                         "notes_from": {"mixim-ai/marketing": "mixim-ai-public"},
                         "conflicts": {"mixim-ai/both": ["mixim-ai-private", "mixim-ai-public"]}}},
 "repos": {"mixim-ai/postal": {"writes": "mixim-ai-public", "reads": []}},
 "folders": {"/Users/kabir/Documents/writing": {"writes": "local-recipes", "reads": []}},
 "default": {"writes": "kabir-personal", "reads": []},
 "plugins": {"basic-memory": {"kind": "basic-memory", "tool_prefixes": ["mcp__basic-memory__"], "data": {}}}}
```

## Plugins

Plugins are built in. Basic Memory (`plugins/basic_memory.py`) answers for its tool calls and sets up its Claude Code plugin. Google Drive (`plugins/gdrive.py`) is a source kind, `gdrive`. The plugin API is described at the top of `plugins/__init__.py`.

## Known problems

- **Read-only access hides the audience.** GitHub only lists a private repo's collaborators to people who can push. Someone with read access to a vault with a source gets "unknown" for it, so after reading it, shared writes ask.
- **GitHub Actions minutes.** Each hourly refresh takes a minute or two.
- **The bot account** needs a Google Workspace seat, and should be a member of the shared drive only.

## Tests

`uv run pytest -q` and `tests/e2e.sh`. Tests set `VAULTLINES_HOME` (in place of `~/.vaultlines`), `VAULTLINES_FAKE_GITHUB` (a JSON file and bare repos, with `VAULTLINES_FAKE_LOGIN`), `VAULTLINES_FAKE_GOOGLE` (a fake Google on 127.0.0.1), and `VAULTLINES_TEST_REMOTES=1` (`file://` remotes count as GitHub, and a local folder as a shared drive).
