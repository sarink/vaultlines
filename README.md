# vaultlines (`vl`)

`vl` gives Claude Code a place to save notes, and the notes it may read.
The notes live in **vaults**. A vault is a folder of markdown notes, kept in a git repo on GitHub.
GitHub decides who gets which vault, and `vl` stops a Claude session from copying notes to people who can't see them.

## Start

```bash
uv tool install vaultlines
vl org join mixim-ai
cd ~/code/marketing && claude
```

That's all. `vl org join` logs you in to GitHub (if needed), finds the `mixim-ai` vaults you can access, and clones them. It also sets up Claude Code, Basic Memory and a background sync.

## How vl picks the vaults for a session

When Claude starts, `vl` answers two questions:

- **writes:** where does Claude save notes? (one vault)
- **reads:** what else may Claude read? (a list of vaults)

It finds the answers in four steps:

1. **Which repo is this?** `vl` finds the git repo of the folder where Claude started, and reads its GitHub address, for example `github.com/mixim-ai/marketing`. If the repo has several remotes, `origin` counts first.
2. **Did you join that owner?** If yes:
   - writes: the `mixim-ai` vault that lists `mixim-ai/marketing` in its `notes_from`. If no vault lists it: your personal vault for `mixim-ai`.
   - reads: all the other `mixim-ai` vaults on your computer.
   - An entry `[repos."mixim-ai/marketing"]` in your `config.toml` wins over this.
3. **If not:** a `[folders."path"]` entry in your `config.toml`, if one covers the folder.
4. **If nothing matched:** writes to your own personal vault. Reads nothing else.

Examples, for Kabir:

| Claude starts in | Saves notes to | May also read |
|---|---|---|
| `~/code/marketing` (repo `mixim-ai/marketing`) | `mixim-ai/vault-public` | the other `mixim-ai` vaults |
| `~/code/sheety` (repo `mixim-ai/sheety`, in no `notes_from`) | `mixim-ai/vault-kabir-personal` | the other `mixim-ai` vaults |
| `~/Desktop` (no repo) | `kabir/vault-kabir-personal` | nothing |

The same repo gets the same vaults wherever it is cloned.
Owners stay apart: in a `mixim-ai` repo, Claude only uses `mixim-ai` vaults.
If two vaults list the same repo in `notes_from`, notes go to your personal vault, and `vl status` shows the conflict.

At the start of each session, `vl` tells Claude where to save notes:

> vaultlines: save notes from this repo to `mixim-ai-public` (Basic Memory project="mixim-ai-public"): Notes everyone at Mixim can see. You can also read: `mixim-ai-private` (Founders' notes…), `mixim-ai-hq` (The text of every file in…). Writing to those asks first. Other vaults are blocked here.

`mixim-ai-public` is the vault's short name: the owner, then the repo name without `vault-`.

## Vaults

A vault is a repo of notes named `OWNER/vault-NAME`, with a file `vault.toml` at its root:

```toml
# mixim-ai/vault-public : vault.toml
about      = "Notes everyone at Mixim can see."
notes_from = ["mixim-ai/marketing", "mixim-ai/studio"]   # repos whose notes go here
```

A new vault is **local**: it is on your computer only. When you **publish** it, it becomes a private repo on GitHub.

```bash
vl vault create mixim-ai/vault-design --about "Design notes." --notes_from mixim-ai/studio
vl vault publish mixim-ai/vault-design          # or add --publish to vault create
```

The flags of `vault create` are the keys of `vault.toml`: `--about`, `--notes_from` (give it again for each repo).

Who can use a published vault is who can access its repo on GitHub. To give someone a vault, give them access to the repo. They get it on their next `vl sync`.

**Personal vaults.** For each org you join, you have a personal vault: `mixim-ai/vault-kabir-personal`. Like any new vault, it stays on your computer until you publish it. In an organization, the org's owners can see every published vault.

## Sources

A vault can be filled from a **source**, like a Google Drive shared drive. It then holds one note for each file in the source: the file's text, and where the original is. Its **fill job**, a GitHub Action, refreshes it every hour. In Claude sessions, the vault is read-only.

Make a vault with a source (for admins, once, on any computer):

```bash
vl vault create mixim-ai/vault-hq --source gdrive --shared_drive "Mixim HQ" \
  --google_client_id 1234-abc.apps.googleusercontent.com --google_client_secret GOCSPX-...
```

After `--source KIND`, each flag is a key of the `[source]` table in `vault.toml`, spelled the same. `vl vault create --source KIND --help` lists them. A vault with a source is always published, because its fill job runs on GitHub.

| Command | What it does |
|---|---|
| `vl source refresh VAULT` | Start the fill job now. `--force` rebuilds every note from scratch. |
| `vl source fetch VAULT PATH` | Fetch one original. Claude runs this when a note isn't enough. |
| `vl source login VAULT` | Log in to the source again, for fetching. |

### Kind `gdrive`: a Google Drive shared drive

| Key | What it is |
|---|---|
| `shared_drive` | The shared drive's name (or ID). |
| `folder` | Only this folder of the drive. Default: the whole drive. |
| `max_size` | Bigger files get a note without text. Default: `"50M"`. |
| `google_client_id` | The client ID of a Google OAuth app of type "Desktop". |
| `google_client_secret` | Its secret. Google doesn't treat a desktop app's secret as secret. |

`vl vault create` asks you to log in as a **bot account**: a Google account that is a member of the shared drive only. It checks that the login can only read Drive.

A note looks like this:

```yaml
---
title: "Runway"
path: "Finance/Runway.xlsx"
text: "full"
fetch: "vl source fetch mixim-ai/vault-hq \"Finance/Runway.xlsx\""
---
| Month | Cash | ...
```

To fetch an original, `vl` asks you to log in to Google once, with read-only access. The copy goes to `~/.vaultlines/cache/fetch/` and is deleted after a day. You only get files that your own Google account can open.

## Your config.toml

`~/.vaultlines/config.toml` holds only your own changes. `vl` works without any. Run `vl apply` after editing.

```toml
# Change the rules for one repo, wherever it is cloned.
[repos."mixim-ai/postal"]
writes    = "mixim-ai/vault-public"
reads     = ["kabir/vault-recipes"]    # added to the owner's vaults
auto_pull = true                       # `git pull --ff-only` it on every sync

# Let Claude read another owner's vault in every mixim-ai repo.
[repos."mixim-ai/*"]
reads = ["kabir/vault-side"]

# Let sessions in a repo run vl commands without asking (for working on vl itself).
[repos."sarink/vaultlines"]
allow_vl_commands = true

# For folders that aren't in a repo of an org you joined.
[folders."~/Documents/writing"]
writes = "kabir/vault-recipes"
```

Settings go at the top of the file: `sync_interval` (600 seconds), `check_interval` (86400), `on_leak` (`"ask"` or `"block"`), `basic_memory` (`true`).

## Safety

`vl` adds a hook to Claude Code. The hook checks each tool call that touches a vault.

- **Focus:** a session can only use its own vaults. Other vaults are blocked.
- **The session label:** `vl` remembers who can see everything the session has read. After Claude reads a vault that fewer people can see, a write to a vault that more people can see asks you first.

Example: Claude reads `mixim-ai/vault-private` (Kabir and Jorge). Then it wants to write to `mixim-ai/vault-public` (everyone at Mixim). `vl` asks: "This session read mixim-ai-private. ana and raj would see this in mixim-ai-public."

`vl` also protects itself. Claude can't write its records or touch your Google login. Changes to `config.toml`, and commands like `vl org` or `vl apply`, ask first unless your message mentions vl (or the repo has `allow_vl_commands = true`).

Limits, honestly:

- Checks of Bash commands are best effort. A command can build a path in ways no check sees.
- Notes are stored on GitHub. GitHub, and the owners of an organization, can read them.
- With read-only access, GitHub won't say who else can see a repo. `vl` then treats it as "only you", so later shared writes ask.

## Where vl keeps things

```
~/.vaultlines/
  config.toml                 your changes (comments and examples until you add some)
  vaults/
    kabir/vault-kabir-personal/
    mixim-ai/                 exists = joined
      vault-public/  vault-private/  vault-hq/  vault-kabir-personal/
  google/                     your read-only Google login, for fetching
  state/                      runtime.json, sessions/, state.json, sync.log
  cache/fetch/                fetched originals, deleted after a day
```

`vl` also adds its own entries to `~/.claude/settings.json` (hooks), to `<repo>/.claude/settings.local.json` (the Basic Memory project there), to Basic Memory's and Obsidian's lists of vaults, and to `~/Library/LaunchAgents` (background sync).

## Commands

| Command | What it does |
|---|---|
| `vl init [--publish]` | Set up this computer: GitHub login, your personal vault, hooks, sync. `--publish` also publishes your personal vault. |
| `vl org join ORG` | Get the vaults of a GitHub organization (or user) that you can access. Runs `vl init` first if needed. |
| `vl org leave ORG` | Stop using an org's vaults. Files move to `~/.vaultlines/left/` (or `--delete-files`). |
| `vl vault create VAULT` | A new vault, on this computer. `--about`, `--notes_from`, `--publish`, `--source KIND`. |
| `vl vault publish VAULT` | Put a local vault on GitHub, as a private repo. |
| `vl source refresh` / `fetch` / `login` | Vaults with a source (see above). |
| `vl sync [VAULT]` | Sync now. Once a day, it also checks with GitHub (`--check-github`: now). |
| `vl status` | Vaults by org, who can see each one, and where writes will ask. |
| `vl doctor` | What's broken, and how to fix it. |
| `vl apply` | Set everything up again, after editing `config.toml`. |
| `vl sessions` | Recent sessions and what they read. |
| `vl uninstall` | Remove the hooks and stop the sync. Notes stay. |

A `VAULT` is always `OWNER/vault-NAME`, or its short name (`mixim-ai-public`).

More details: [docs/reference.md](docs/reference.md).
