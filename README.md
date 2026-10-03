# vaultlines (`vl`)

`vl` gives Claude Code a place to save notes, and the notes it may read.
The notes live in **vaults**. A vault is a folder of markdown notes, kept in a git repo on GitHub.
GitHub decides who gets which vault, and `vl` stops a Claude session from copying notes to people who can't see them.

## Start

```bash
uv tool install vaultlines
vl join acme
cd ~/code/marketing && claude
```

That's all. `vl join` logs you in to GitHub (if needed), finds the `acme` vaults you can access, and clones them. It also sets up Claude Code, Basic Memory and a background sync. Once a day, the sync checks with GitHub again: it clones new vaults, and stops syncing a vault you can't access any more (its files stay, and sessions can't use it).

## Words

- **Owner:** a GitHub user or organization, like `acme`. You join owners, and their vaults never mix.
- **Vault ID:** `OWNER/REPO`, like `acme/vault-public`. It's a vault's only name: in commands, in Claude's sessions, and in Basic Memory.
- **Local** and **published:** a new vault is local, on your computer only. A published vault is a private repo on GitHub.
- **Source:** where a vault's notes come from, like a Google Drive folder. A vault with a source is read-only in sessions.
- **Refresh job:** the GitHub Action that refreshes a vault from its source every hour.

## How vl picks the vaults for a session

When Claude starts, `vl` answers two questions:

- **writes:** where does Claude save notes? (one vault)
- **reads:** what else may Claude read? (a list of vaults)

It finds the answers in four steps:

1. **Which repo is this?** `vl` finds the git repo of the folder where Claude started, and reads its GitHub address, for example `github.com/acme/marketing`. If the repo has several remotes, `origin` counts first.
2. **Did you join that owner?** If yes:
   - writes: the `acme` vault that lists `acme/marketing` in its `notes_from`. If no vault lists it: your personal vault for `acme`.
   - reads: all the other `acme` vaults on your computer.
   - An entry `[repos."acme/marketing"]` in your `config.toml` wins over this.
3. **If not:** a `[folders."path"]` entry in your `config.toml`, if one covers the folder.
4. **If nothing matched:** writes to your own personal vault. Reads nothing else.

Examples, for Kabir:

| Claude starts in | Saves notes to | May also read |
|---|---|---|
| `~/code/marketing` (repo `acme/marketing`) | `acme/vault-public` | the other `acme` vaults |
| `~/code/billing` (repo `acme/billing`, in no `notes_from`) | `acme/vault-kabir-personal` | the other `acme` vaults |
| `~/Desktop` (no repo) | `kabir/vault-kabir-personal` | nothing |

The same repo gets the same vaults wherever it is cloned.
Owners stay apart: in an `acme` repo, Claude only uses `acme` vaults.
If two vaults list the same repo in `notes_from`, notes go to your personal vault, and `vl status` shows the conflict.

At the start of each session, `vl` tells Claude where to save notes:

> vaultlines: save notes from this repo to `acme/vault-public` (Basic Memory project="acme/vault-public"): Notes everyone at Acme can see. You can also read: `acme/vault-hq` (The text of every file in Acme HQ, in Google Drive. Claude…), `acme/vault-kabir-personal`, `acme/vault-private` (Founders' notes.). Writing to those asks first. Other vaults are blocked here. Always pass project="..." to Basic Memory tools. `acme/vault-hq` holds notes converted from Google Drive; for an original, run `vl source fetch acme/vault-hq "<path from the note's frontmatter>"`.

## Vaults

A vault is a repo of notes named `OWNER/vault-NAME`, with a file `vault.toml` at its root:

```toml
# acme/vault-public : vault.toml
about      = "Notes everyone at Acme can see."
notes_from = ["acme/marketing", "acme/studio"]   # repos whose notes go here
```

A new vault is local. Publish it to put it on GitHub:

```bash
vl vault create acme/vault-design --about "Design notes." --notes_from acme/studio
vl vault publish acme/vault-design
```

A flag with `_` sets the key of `vault.toml` with the same name: `--about`, `--notes_from` (give it again for each repo). A flag with `-`, like `--delete-files`, is only an option of its command.

Who can use a published vault is who can access its repo on GitHub. To give someone a vault, give them access to the repo. They get it on their next `vl sync`.

**Personal vaults.** For each owner you join, you have a personal vault: `acme/vault-kabir-personal`. Like any new vault, it's local until you publish it. In an organization, its owners can see every published vault.

## Sources

A vault's notes can come from a source. The vault then holds one note for each file in the source: the file's text, and where the original is. Its refresh job refreshes it every hour, so a vault with a source is always published.

An admin makes one, once, on any computer:

```bash
vl vault create acme/vault-hq --source gdrive --client_id 1234-abc.apps.googleusercontent.com --client_secret GOCSPX-...
```

What you need first, and how to get it, is in `vl vault create --source gdrive --help`. If you leave out a key, `vl` shows those steps and asks for it. After `--source KIND`, each flag is a key of the `[source]` table in `vault.toml` (see [the reference](docs/reference.md)).

For `gdrive`, `vl vault create` asks you to log in to Google as the refresh job's account, and checks that the login can only read Drive. If you left out `--folder_id`, it lists what the account can open, by name: its shared drives, the folders shared with it, and My Drive. You pick one, then go down its folders:

```
This account can open:
  1. Acme HQ (shared drive)
  2. My Drive
folder_id: where are the vault's files: 1
  1. All of Acme HQ
  2. Finance/
  3. Legal/
  4. (back)
Acme HQ: all of it, or a folder in it: 2
```

`vault.toml` gets the folder's ID, with its name as a comment. The vault gets its refresh job, `.github/workflows/vl-source.yml`, and the account's read-only login, in the Actions secret `VL_SOURCE_TOKEN`. Everyone who can read the vault reads the text of every file in the folder.

**A refresh** has two halves, so the code that converts files never runs where the login is. The refresh job runs them as two steps:

```bash
VL_SOURCE_TOKEN=... vl source refresh --fetch-only   # with the login: list the source, download what changed
vl source refresh --convert-only                     # without it: write the notes, commit and push
```

The fetch checks that the login can only read, and downloads new and changed files, at most 5 GB in one refresh; the rest waits for the next one. The convert turns them into text with markitdown, writes one note per file (keeping keys that others added to a note's frontmatter), and commits "Update from Google Drive". If another refresh pushed first, its commit goes on top.

`vl source refresh` alone does both halves, on any computer. It logs in with `VL_SOURCE_TOKEN` if that's set, or else with your own login (`vl source login`). To start the refresh job on GitHub now: `gh workflow run vl-source.yml --repo acme/vault-hq` (add `-f force=true` to rebuild every note).

On your computer, `vl sync` only pulls a vault with a source. Changes made to it here are saved on a branch `local-changes-DATE`, and the vault is reset to GitHub's.

A note looks like this:

```yaml
---
title: "Runway"
path: "Finance/Runway.xlsx"
text: "full"
fetch: "vl source fetch acme/vault-hq \"Finance/Runway.xlsx\""
---
| Month | Cash | ...
```

When a note isn't enough, Claude runs its `fetch` command. `vl source fetch` downloads the original by its Drive ID, so renames don't break it, and exports Google's own files: Docs to `.docx`, Sheets to `.xlsx`, Slides to `.pptx`, Drawings to `.pdf`. It uses your own read-only Google login, so you only get files that your Google account can open. The copy goes to `~/.vaultlines/cache/fetch/` and is deleted after a day.

## Your config.toml

`~/.vaultlines/config.toml` holds only your own changes. `vl` works without any. Run `vl apply` after editing.

```toml
# Change the rules for one repo, wherever it is cloned.
[repos."acme/website"]
writes    = "acme/vault-public"
reads     = ["kabir/vault-recipes"]    # added to the owner's vaults
auto_pull = true                       # `git pull --ff-only` it on every sync

# Let Claude read another owner's vault in every acme repo.
[repos."acme/*"]
reads = ["kabir/vault-side"]

# Dev mode, for working on vl itself: the hook guards nothing in sessions in this repo.
[repos."sarink/vaultlines"]
dangerously_skip_hook_guards = true

# For folders that aren't in a repo of an owner you joined.
[folders."~/Documents/writing"]
writes = "kabir/vault-recipes"
```

Settings go at the top of the file: `sync_interval` (600 seconds), `check_interval` (86400), `on_leak` (`"ask"` or `"block"`), `basic_memory` (`true`).

## Safety

`vl` adds a hook to Claude Code. The hook checks each tool call that touches a vault.

- **Focus:** a session can only use its own vaults. Other vaults are blocked.
- **The session label:** `vl` remembers who can see everything the session has read. After Claude reads a vault that fewer people can see, a write to a vault that more people can see asks you first.

Example: Claude reads `acme/vault-private` (Kabir and Lee). Then it wants to write to `acme/vault-public` (everyone at Acme). `vl` asks: "This session read acme/vault-private. ana and raj would see this in acme/vault-public."

`vl` also protects itself. Claude can't write its records or touch your Google login. Changes to `config.toml`, and commands like `vl join` or `vl apply`, ask first unless your message mentions vl.

**Dev mode**, for working on vl itself: `dangerously_skip_hook_guards = true` in a repo's entry in `config.toml`. In sessions in that repo, the hook guards nothing: Claude can read and change every vault and all of `vl`'s own files, your Google logins too, and nothing asks first.

Limits, honestly:

- Checks of Bash commands are best effort. A command can build a path in ways no check sees.
- Notes are stored on GitHub. GitHub, and the owners of an organization, can read them.
- With read-only access, GitHub won't say who else can see a repo. `vl` then treats it as "only you", so later shared writes ask.
- Anyone who can push to a vault with a source can use its refresh job's login, and read everything that login can read.

## Where vl keeps things

```
~/.vaultlines/
  config.toml                 your changes (comments and examples until you add some)
  vaults/
    kabir/vault-kabir-personal/
    acme/                     exists = joined
      vault-public/  vault-private/  vault-hq/  vault-kabir-personal/
  left/                       the files of owners you left
  google/                     your own Google login, for fetching and refreshing
  state/                      runtime.json, sessions/, state.json, sync.log
  cache/fetch/                fetched originals, deleted after a day
  cache/refresh/              what a refresh fetched, until it's converted
```

`vl` also adds its own entries to `~/.claude/settings.json` (hooks), to `<repo>/.claude/settings.local.json` (the Basic Memory project there), to Basic Memory's and Obsidian's lists of vaults, and to `~/Library/LaunchAgents` (background sync).

## Commands

| Command | What it does |
|---|---|
| `vl init` | Set up this computer: GitHub login, your personal vault, hooks, background sync. |
| `vl join OWNER` | Get the owner's vaults that you can access. Runs `vl init` first if needed. |
| `vl leave OWNER [--delete-files]` | Stop using the owner's vaults. Their files move to `~/.vaultlines/left/`, or are deleted. |
| `vl vault create VAULT [--about TEXT] [--notes_from REPO]... [--source KIND [--KEY VALUE]...]` | Make a vault. It's local, unless it has a source. |
| `vl vault publish VAULT` | Put a local vault on GitHub, as a private repo. |
| `vl source refresh [VAULT] [--fetch-only \| --convert-only] [--force]` | Refresh a vault from its source, on this computer. Without VAULT: the vault this folder is in. `--force` rebuilds every note. |
| `vl source fetch VAULT PATH` | Fetch one original. |
| `vl source login VAULT` | Log in to the vault's source with your own account, for fetching originals and refreshing on this computer. |
| `vl sync [VAULT] [--check-github]` | Sync now. Once a day, it also checks with GitHub; `--check-github` checks now. |
| `vl status` | Vaults by owner, who can see each one, and where writes will ask. |
| `vl doctor` | What's broken, and how to fix it. |
| `vl apply` | Set everything up again, after editing `config.toml`. |
| `vl sessions [--limit N]` | Recent sessions and what they read. |
| `vl uninstall` | Remove the hooks and stop the sync. Notes stay. |

`VAULT` is always a vault ID, `OWNER/REPO`.

More details: [docs/reference.md](docs/reference.md).
