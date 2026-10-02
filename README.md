# vaultlines (`vl`)

`vl` gives Claude Code a place to save notes, and the notes it may read.
The notes live in **vaults**. A vault is a folder of markdown notes, kept in a git repo on GitHub.
GitHub decides who gets which vault, and `vl` stops a Claude session from copying notes to people who can't see them.

## Start

```bash
uv tool install vaultlines
vl org join acme
cd ~/code/marketing && claude
```

That's all. `vl org join` logs you in to GitHub (if needed), finds the `acme` vaults you can access, and clones them. It also sets up Claude Code, Basic Memory and a background sync.

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
Owners stay apart: in a `acme` repo, Claude only uses `acme` vaults.
If two vaults list the same repo in `notes_from`, notes go to your personal vault, and `vl status` shows the conflict.

At the start of each session, `vl` tells Claude where to save notes:

> vaultlines: save notes from this repo to `acme-public` (Basic Memory project="acme-public"): Notes everyone at Acme can see. You can also read: `acme-private` (Founders' notes…), `acme-hq` (The text of every file in…). Writing to those asks first. Other vaults are blocked here.

`acme-public` is the vault's short name: the owner, then the repo name without `vault-`.

## Vaults

A vault is a repo of notes named `OWNER/vault-NAME`, with a file `vault.toml` at its root:

```toml
# acme/vault-public : vault.toml
about      = "Notes everyone at Acme can see."
notes_from = ["acme/marketing", "acme/studio"]   # repos whose notes go here
```

A new vault is **local**: it is on your computer only. When you **publish** it, it becomes a private repo on GitHub.

```bash
vl vault create acme/vault-design --about "Design notes." --notes_from acme/studio
vl vault publish acme/vault-design          # or add --publish to vault create
```

The flags of `vault create` are the keys of `vault.toml`: `--about`, `--notes_from` (give it again for each repo).

Who can use a published vault is who can access its repo on GitHub. To give someone a vault, give them access to the repo. They get it on their next `vl sync`.

**Personal vaults.** For each org you join, you have a personal vault: `acme/vault-kabir-personal`. Like any new vault, it stays on your computer until you publish it. In an organization, the org's owners can see every published vault.

## Sources

A vault's notes can come from a **source**, like a Google Drive folder. It then holds one note for each file in the source: the file's text, and where the original is. Its **refresh job**, a GitHub Action, refreshes it every hour. In Claude sessions, the vault is read-only.

Make a vault with a source (for admins, once, on any computer):

```bash
vl vault create acme/vault-hq --source gdrive \
  --google_client_id 1234-abc.apps.googleusercontent.com --google_client_secret GOCSPX-...
```

After `--source KIND`, each flag is a key of the `[source]` table in `vault.toml`, spelled the same. `vl vault create --source KIND --help` lists them, and says how to get each one. If you leave out a key, `vl` shows those steps and asks for it. A vault with a source is always published, because its refresh job runs on GitHub.

| Command | What it does |
|---|---|
| `vl source refresh [VAULT]` | Refresh the vault on this computer: fetch what changed, convert it, commit and push. Without VAULT: the vault this folder is in. `--force` rebuilds every note from scratch. |
| `vl source fetch VAULT PATH` | Fetch one original. Claude runs this when a note isn't enough. |
| `vl source login VAULT` | Log in to the source again, for fetching. |

`vl source refresh` logs in with `VL_SOURCE_TOKEN` if it's set, or else with your own login (`vl source login`). It has two halves, and `--fetch-only` and `--convert-only` run one of them. The refresh job runs them as two steps, so the code that converts files never runs where the login is:

```bash
VL_SOURCE_TOKEN=... vl source refresh --fetch-only   # with the login: download what changed
vl source refresh --convert-only                     # without it: write the notes, commit and push
```

To start the refresh job on GitHub now: `gh workflow run vl-source.yml --repo acme/vault-hq`.

### Kind `gdrive`: a Google Drive folder

| Key | What it is |
|---|---|
| `folder_id` | The folder, or a whole shared drive. `--folder_id` takes its URL (like `https://drive.google.com/drive/folders/1AbC…`) or its ID. Leave it out and `vl` lists them. |
| `max_size` | Bigger files get a note without text. Default: `"50M"`. |
| `google_client_id` | The client ID of a Google OAuth app of type "Desktop". |
| `google_client_secret` | Its secret. Google doesn't treat a desktop app's secret as secret. |

Before you start, you need two things. A Google Workspace account and a personal Gmail account both work.

1. **A Google OAuth app** (about 5 minutes):
   1. Make a project: <https://console.cloud.google.com/projectcreate>. With Workspace, for "Location", pick your organization.
   2. Turn on the Drive API: <https://console.cloud.google.com/apis/library/drive.googleapis.com>, then "Enable".
   3. Set up the login screen: <https://console.cloud.google.com/auth/overview>, then "Get started". With Workspace, Audience: "Internal". With a personal Gmail account, Audience: "External", then "Publish app" on the "Audience" page. While the app is "Testing", Google ends each login after 7 days.
   4. Make the client: <https://console.cloud.google.com/auth/clients>, then "Create client". Application type: "Desktop app". Google then shows the client ID and the client secret.
2. **A Google account for the refresh job.** We recommend a bot account that can open only the vault's folder: make a user at <https://admin.google.com> (Directory > Users), or a new Gmail account. Share the folder with it as a "Viewer" (or add it to the shared drive as a "Viewer"), and share nothing else with it. Any account works, like your own. But anyone who can push to the vault's repo can use its login to read everything that account can read in Drive.

`vl vault create` asks you to log in as that account. It checks that the login can only read Drive. If you left out `--folder_id`, it lists what the account can open by name: its shared drives, the folders shared with it, and My Drive. You pick one, then go down its folders:

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

`vault.toml` gets the folder's ID, with its name as a comment. Everyone who can read the vault reads the text of every file in the folder.

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

To fetch an original, `vl` asks you to log in to Google once, with read-only access. The copy goes to `~/.vaultlines/cache/fetch/` and is deleted after a day. You only get files that your own Google account can open.

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

Example: Claude reads `acme/vault-private` (Kabir and Lee). Then it wants to write to `acme/vault-public` (everyone at Acme). `vl` asks: "This session read acme-private. ana and raj would see this in acme-public."

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
    acme/                 exists = joined
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

A `VAULT` is always `OWNER/vault-NAME`, or its short name (`acme-public`).

More details: [docs/reference.md](docs/reference.md).
