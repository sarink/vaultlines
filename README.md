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

That's all. `vl org join` signs you in to GitHub (if needed), finds the `mixim-ai` vaults you can access, and clones them. It also sets up Claude Code, Basic Memory and a background sync.

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

A vault is a GitHub repo whose name starts with `vault-`, with a file `vault.toml` at its root:

```toml
# mixim-ai/vault-public : vault.toml
about      = "Notes everyone at Mixim can see."
notes_from = ["mixim-ai/marketing", "mixim-ai/studio"]   # repos whose notes go here
```

Who can use a vault is who can access its repo on GitHub. To give someone a vault, give them access to the repo. They get it on their next `vl sync`.

Make a new vault (for admins):

```bash
vl vault create mixim-ai/vault-design --about "Design notes."
```

**Personal vaults.** Each owner you join has a personal vault for you: `mixim-ai/vault-kabir-personal`. `vl` makes it on your computer only. To put it on GitHub, as a private repo:

```bash
vl vault publish mixim-ai/vault-kabir-personal
```

Anyone with access to a published personal vault gets it. In an organization, its owners can always see it.

**Local vaults** stay on your computer, with no GitHub repo: `vl vault create local/recipes`.

## Google Drive vaults

A Drive vault holds one note for each file in a shared drive: the file's text, and a link to the original.
A GitHub Action fills it every hour, signed in as a bot account that can only read the drive. In Claude sessions, the vault is read-only.

A note looks like this:

```yaml
---
title: "Runway"
path: "Finance/Runway.xlsx"
text: "full"
fetch: "vl gdrive fetch mixim-ai/vault-hq \"Finance/Runway.xlsx\""
---
| Month | Cash | ...
```

To get the original file, Claude runs the `fetch` command. `vl` asks you to sign in to Google once, with read-only access. The copy goes to `~/.vaultlines/cache/fetch/` and is deleted after a day. You only get files that your Google account can open.

Set one up (for admins, once, on any computer):

```bash
vl gdrive add mixim-ai/vault-hq --shared-drive "Mixim HQ" \
  --client-id 1234-abc.apps.googleusercontent.com --client-secret GOCSPX-...
```

This asks you to sign in as the bot account, checks that it can only read, and creates the repo with its `vault.toml` and workflow. Then it starts the first run. `vl gdrive rebuild mixim-ai/vault-hq` writes every note again.

## Your config.toml

`~/.vaultlines/config.toml` holds only your own changes. `vl` works without any. Run `vl apply` after editing.

```toml
# Vaults on this computer only.
[vaults."local/recipes"]

# Change the rules for one repo, wherever it is cloned.
[repos."mixim-ai/postal"]
writes    = "mixim-ai/vault-public"
reads     = ["local/recipes"]          # added to the owner's vaults
auto_pull = true                       # `git pull --ff-only` it on every sync

# Let Claude read another owner's vault in every mixim-ai repo.
[repos."mixim-ai/*"]
reads = ["kabir/vault-side"]

# For folders that aren't in a repo of an owner you joined.
[folders."~/Documents/writing"]
writes = "local/recipes"
```

Settings go at the top of the file: `sync_interval` (600 seconds), `check_interval` (86400), `on_leak` (`"ask"` or `"block"`), `basic_memory` (`true`).

## Safety

`vl` adds a hook to Claude Code. The hook checks each tool call that touches a vault.

- **Focus:** a session can only use its own vaults. Other vaults are blocked.
- **The session label:** `vl` remembers who can see everything the session has read. After Claude reads a vault that fewer people can see, a write to a vault that more people can see asks you first.

Example: Claude reads `mixim-ai/vault-private` (Kabir and Jorge). Then it wants to write to `mixim-ai/vault-public` (everyone at Mixim). `vl` asks: "This session read mixim-ai-private. ana and raj would see this in mixim-ai-public."

`vl` also protects itself. Claude can't write its records or touch your Google sign-in. Changes to `config.toml`, and commands like `vl org` or `vl apply`, ask first unless your message mentions vl.

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
    local/recipes/
  google/                     your read-only Google sign-in, for fetching
  state/                      runtime.json, sessions/, state.json, sync.log
  cache/fetch/                fetched originals, deleted after a day
```

`vl` also adds its own entries to `~/.claude/settings.json` (hooks), to `<repo>/.claude/settings.local.json` (the Basic Memory project there), to Basic Memory's and Obsidian's lists of vaults, and to `~/Library/LaunchAgents` (background sync).

## Commands

| Command | What it does |
|---|---|
| `vl org join OWNER` | Get the vaults of a GitHub organization or user. Runs `vl init` first if needed. |
| `vl org leave OWNER` | Stop using an owner's vaults. Files move to `~/.vaultlines/left/` (or `--delete-files`). |
| `vl init [--publish]` | Set up this computer: GitHub sign-in, your personal vault, hooks, sync. |
| `vl vault create OWNER/vault-NAME` | A new private vault repo with `vault.toml`. |
| `vl vault publish OWNER/vault-ME-personal` | Put your personal vault on GitHub, private. |
| `vl gdrive add` / `fetch` / `login` / `rebuild` | Google Drive vaults (see above). |
| `vl sync` | Sync now. Once a day it also asks GitHub for new vaults. |
| `vl status`, `vl check`, `vl doctor` | What's set up; who can see each vault; what's broken. |
| `vl apply` | Set everything up again, after editing `config.toml`. |
| `vl sessions` | Recent sessions and what they read. |
| `vl uninstall` | Remove the hooks and stop the sync. Notes stay. |

More details: [docs/reference.md](docs/reference.md).
