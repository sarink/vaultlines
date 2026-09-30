# vaultlines

**Personal and team memory vaults for Claude Code. Each folder declares what it
writes and reads, and GitHub's real access lists keep notes from leaking.**

`vl` gives Claude Code a long-term memory made of plain markdown files, split into
**vaults**: one for you, and others you share with teams, clients or friends. Each
folder you work in says which vault it **writes** to and which vaults it **reads**.
Before setting a folder up, `vl` asks GitHub who can see each vault. It refuses any
folder where Claude could copy notes to people who couldn't see them before.

Under the hood it wires together three tools you may already use:

- [Basic Memory](https://github.com/basicmachines-co/basic-memory) stores the notes
  and gives Claude fast, offline search over them.
- [Claude Code](https://claude.com/claude-code) settings and permission rules
  decide which vaults each folder can use.
- **git and GitHub** sync each vault, and GitHub's access lists say who can see it.

Vaults are ordinary folders, so you can also open them in
[Obsidian](https://obsidian.md).

## An example

Sam co-founded Acme, does client work for Globex, and has a side project.

```toml
# ~/.config/vaultlines/config.toml

[settings]
sync_interval = 600

# ---------------------------------------------------------------- vaults
# Remotes are GitHub repos. A vault with no remote stays on this computer.

[vaults.personal]
path   = "~/Vaults/personal"
remote = "https://github.com/sam/vault-personal.git"

[vaults.side-project]
path   = "~/Vaults/side-project"            # no remote: this computer only

[vaults.acme-founders]
path   = "~/Vaults/acme-founders"
remote = "https://github.com/acme/acme-founders.git"

[vaults.acme-everyone]
path   = "~/Vaults/acme-everyone"
remote = "https://github.com/acme/acme-everyone.git"

[vaults.globex]
path   = "~/Vaults/globex"
remote = "https://github.com/globex/shared-notes.git"

# ---------------------------------------------------------------- folders
# Each folder writes to one vault and reads a list of others.
# Writing to a vault in `reads` asks first.

[folders."*"]                                # every folder not listed below
writes = "personal"
reads  = []

[folders."~/notes"]                          # a journal that can look up Acme info
writes = "personal"
reads  = ["acme-everyone"]

[folders."~/code/acme-legal"]                # founder work
writes = "acme-founders"
reads  = ["acme-everyone"]

[folders."~/code/acme-app"]                  # everyday Acme work
writes    = "acme-everyone"
reads     = []
auto_pull = true                             # keep this repo pulled

[folders."~/code/side-project"]              # a project only Sam sees
writes = "side-project"
reads  = ["acme-everyone", "personal"]
```

GitHub says who can see each vault:

| Vault | Who can see it |
|---|---|
| `personal` | sam |
| `side-project` | sam (no remote) |
| `acme-founders` | sam, lee |
| `acme-everyone` | sam, lee, ana, raj |
| `globex` | sam, globex-dev1, globex-dev2 |

So every folder above passes:

| Folder | Why it's allowed |
|---|---|
| everywhere else (`"*"`) | Writes to `personal` and reads nothing else. |
| `~/notes` | Only sam sees `personal`, and sam can see `acme-everyone`. |
| `~/code/acme-legal` | Everyone who sees `acme-founders` (sam, lee) can see `acme-everyone`. |
| `~/code/acme-app` | Reads nothing else. |
| `~/code/side-project` | Only sam sees `side-project`, and sam can see everything it reads. |

And these would be refused:

```
$ vl folder set ~/code/acme-app --writes acme-everyone --reads acme-founders
error: ~/code/acme-app can't read acme-founders and write acme-everyone: ana and raj can see acme-everyone but not acme-founders.

$ vl folder set ~/code/globex-api --writes globex --reads acme-everyone
error: ~/code/globex-api can't read acme-everyone and write globex: globex-dev1 and globex-dev2 can see globex but not acme-everyone.

$ vl folder set ~/code/acme-legal --writes acme-founders --reads personal
error: ~/code/acme-legal can't read personal and write acme-founders: lee can see acme-founders but not personal.
```

Later, if Sam gives Ana access to `acme-founders`, nothing changes: Ana can
already see `acme-everyone`. But if Sam invites a contractor to `acme-founders`
only, the daily check notices, takes `~/code/acme-legal` down, and notifies Sam.

## How it works

### Folders declare their vaults

Each entry under `[folders]` says:

- **`writes`**: the one vault this folder writes to. Claude reads and writes it freely.
- **`reads`**: other vaults Claude can read here. Writing to them **asks you first**.
- **`auto_pull`** (optional, default `false`): pull this git repo on every sync.

Every other vault is off limits in that folder. Its tools aren't loaded, and the
`"*"` vaults are blocked with deny rules.

`"*"` covers every folder you haven't listed. Its vaults are set up at the user
level, so they're available everywhere except in listed folders that don't use them.

The approvals are Claude Code **ask rules**, and the blocks are **deny rules**. Both
still apply in bypass-permissions mode.

### The audience check

**Rule: everyone who can see the vault a folder writes to must be able to see every
vault it reads.** Otherwise Claude could copy notes from a read vault into the write
vault, where new people would see them.

Who can see a vault comes from GitHub:

| Vault | Who can see it |
|---|---|
| No remote | Only you |
| Public GitHub repo | Everyone |
| Private repo you can push to | Its collaborators, from GitHub's API. This includes people who get access through the org's base permission, teams, and org owners. |
| Private repo you can only read | Unknown. GitHub won't list collaborators to read-only users. |

A folder passes when:

- its write vault is **only yours** (you can already see everything you read), or
- each vault it reads is **public**, or
- everyone who can see the write vault can also see each read vault.

Anything unknown is **refused**, never guessed. The error names the people who could
see the write vault but not the read vault.

- `vl folder set` refuses and changes nothing.
- `vl apply` sets up every folder that passes and skips the rest, so they use `"*"`
  instead. It prints the errors and exits with an error. If `"*"` itself fails, it
  gets only its write vault.
- **Once a day**, background sync asks GitHub again and re-applies. If a folder stops
  passing, it's taken down and you get a notification. If GitHub can't be reached,
  `vl` uses the last answer it got and tries again at the next sync.

`vl check` shows the whole picture:

```
$ vl check
Who can see each vault
  vault          who can see it
  acme-everyone  ana, lee, raj, sam
  acme-founders  lee, sam
  globex         globex-dev1, globex-dev2, sam
  personal       sam
  side-project   only you (no remote)

Folders
  folder               writes         reads                    auto_pull  check
  "*"                  personal       -                                   ok
  ~/code/acme-app      acme-everyone  -                        yes        ok
  ~/code/acme-legal    acme-founders  acme-everyone                       ok
  ~/code/side-project  side-project   acme-everyone, personal             ok
  ~/notes              personal       acme-everyone                       ok
```

### What Claude does on its own

The [Basic Memory plugin](https://github.com/basicmachines-co/basic-memory/tree/main/plugins/claude-code)
(installed by `vl init`) does two things automatically:

- **At the start of each session,** it loads a short briefing from the folder's
  write vault.
- **Before Claude compacts a long conversation,** it saves a checkpoint note in that
  vault's `sessions/` folder.

`sessions/` is never synced. Checkpoints stay on the computer that wrote them, so
nothing lands in a shared vault by accident. Anything else Claude saves, you asked for,
or your instructions told it to save.

### Sync

A background job (launchd on macOS) runs `vl sync` every 10 minutes. For each vault
it commits changes, pulls with rebase, and pushes. Notes use git's `union` merge, so
when two people edit the same note at the same time, both edits are kept and sync
never stops on a conflict. If sync fails anyway, you get a notification.

Folders with `auto_pull = true` are pulled too, fast-forward only. `vl` never pushes
them. If the pull can't fast-forward, it's skipped and noted in the log.

## Install

You need **git**, **[uv](https://docs.astral.sh/uv/)**,
**[Claude Code](https://claude.com/claude-code)** and the
**[GitHub CLI](https://cli.github.com)** (`gh`), signed in with `gh auth login`.

```bash
uv tool install git+https://github.com/sarink/vaultlines
vl init
```

`vl init` does the following:

1. Sets Basic Memory so it doesn't rewrite notes that other people also edit.
2. Installs the Basic Memory plugin for Claude Code.
3. Creates your personal vault at `~/Vaults/personal`, backed by a new private GitHub
   repo, `vault-personal`. Use `--local` to keep it on this computer only (then you
   don't need `gh`).
4. Adds `"*"`, writing to `personal`.
5. Starts background sync.

## Quick start

**On your own:** after `vl init` you're done. Claude has a personal memory in every
folder.

**Start a team** (for example as a founder):

```bash
vl vault create acme-everyone --github acme/acme-everyone
vl vault create acme-founders --github acme/acme-founders

vl folder set ~/code/acme-legal --writes acme-founders --reads acme-everyone
vl folder set ~/code/acme-app   --writes acme-everyone
```

Then give the team access to `acme/acme-everyone` on GitHub, and give only the
founders access to `acme/acme-founders`.

**Join a team:**

```bash
vl init
vl vault join https://github.com/acme/acme-everyone.git
vl folder set ~/code/acme-app --writes acme-everyone
```

**Move existing notes in:** `vl vault adopt ~/Notes` takes over a folder you already
have, and makes it a git repo if it isn't one. Add `--github OWNER/REPO` to also
create a private GitHub repo for it.

## Commands

| Command | What it does |
|---|---|
| `vl init [--local]` | Set up this computer. Safe to run again. |
| `vl vault create NAME [--github OWNER/REPO \| --local]` | New vault. By default this creates a private GitHub repo `YOU/vault-NAME`. |
| `vl vault join URL [--name NAME]` | Clone a vault from GitHub. |
| `vl vault adopt PATH [--github OWNER/REPO]` | Manage a folder that already has notes. |
| `vl vault remove NAME [--delete-files]` | Stop managing a vault. Its files stay unless you pass `--delete-files`. Folders must stop using it first. |
| `vl folder set PATH --writes V [--reads A,B] [--auto-pull]` | Set a folder's vaults. `PATH` can be `"*"` (quote it). Replaces the folder's entry. |
| `vl folder unset PATH` | Remove a folder's entry, so it uses `"*"`. |
| `vl check` | Ask GitHub who can see each vault, and check every folder. |
| `vl sync [VAULT]` | Sync now. |
| `vl status` | Vaults and who can see them, folders and their check, sync. |
| `vl apply` | Rewrite Claude Code and Obsidian settings from the config. |
| `vl doctor` | Check that everything is set up. |
| `vl uninstall` | Stop background sync. Everything else stays. |

## The config file

Everything lives in `~/.config/vaultlines/config.toml`, one per computer. You can
edit it by hand, then run `vl apply`.

- Every folder needs `writes` and `reads` (`reads` can be `[]`).
- Vault names must exist, and `writes` can't also be in `reads`.
- `auto_pull` only works on git repos, and not on `"*"`.
- `remote` must be `https://github.com/OWNER/REPO` (with or without `.git`). Leave
  it out for a vault that stays on this computer.
- A vault in `"*"`'s `reads` can't be another folder's `writes`. Claude Code applies
  user-level ask rules everywhere, so that folder would have to ask before every
  write.

Mistakes are reported with the file and key, for example
`~/.config/vaultlines/config.toml: folders."~/code/app".reads: no vault named 'acme'`.

Optional settings: `vaults_dir` (default `~/Vaults`) and `bm_command` (default
`uvx basic-memory`). Audiences from GitHub are cached in
`~/.config/vaultlines/state.json`.

## What `vl` changes on your computer

`vl` only touches things it owns, so `vl apply` can rewrite them safely:

| Where | What |
|---|---|
| Claude Code MCP servers | Servers named `vl-<vault>`. The `"*"` vaults are user-level. The others are added only for the folders that use them (local scope, stored in `~/.claude.json`, never in a repo). |
| `~/.claude/settings.json` | The `basicMemory` block, ask rules for `"*"`'s read vaults, and deny rules that stop Claude from adding or deleting Basic Memory projects. All rules start with `mcp__vl-`. |
| `<folder>/.claude/settings.local.json` | The `basicMemory` block, and the ask and deny rules starting with `mcp__vl-`. If the folder is a git repo and the file isn't ignored yet, `vl` adds it to `.git/info/exclude`. |
| Basic Memory | One project per vault. |
| Each vault | A git repo with `.gitignore` (`sessions/`, `.obsidian/`) and `.gitattributes` (`*.md merge=union`). |
| `auto_pull` folders | `git pull --ff-only` on every sync. Nothing else. |
| Obsidian | Adds each vault to the vault switcher, only while Obsidian is closed. |
| macOS | `~/Library/LaunchAgents/com.vaultlines.sync.plist`, logging to `~/Library/Logs/vaultlines.log`. |

Your other settings, rules and servers are left alone.

## Security model

- **The real boundary is which vaults exist on a computer.** Someone who never gets
  access to the `acme-founders` repo can't read it. Give GitHub access with that in
  mind.
- **The audience check covers vaults, not repos.** In `~/code/acme-app`, Claude also
  writes code, docs and commit messages into that repo, and it could copy a note
  from a read vault into them. That goes through your normal diff and pull request
  review, so review what Claude writes before it's pushed. A repo where Claude
  pushes straight to `main` gets no review step.
- **Folder rules prevent mistakes; they are not a sandbox.** They stop Claude from
  using the wrong vault through its tools. They can't stop a person, or a shell
  command, from copying files.
- **You are still a channel.** If you tell a session in a shared folder something
  private, Claude can write it to the shared vault.
- **GitHub's list is the whole list, with limits.** Collaborators include org base
  permissions, teams and owners. Deploy keys and GitHub Apps with access to a repo
  aren't people and aren't listed. Invitations count once they're accepted, at the
  next daily check.
- **Read-only vaults can block shared writers.** If you can only read a private vault,
  GitHub won't say who else can see it. A folder that writes to a shared vault can't
  read it. A folder that writes only to your own vaults can.
- **`vl` checks that each vault's git remote matches the config.** If they don't
  match, the vault's audience is unknown, so folders that depend on it are refused.
- **GitHub stores your notes.** It holds a copy, even of private repos. For notes that
  should never leave your computer, leave out `remote`.
- **Leaving a team doesn't delete notes.** When someone leaves, remove their repo
  access. The copy already on their computer stays.

## A team workspace (optional)

`vl` handles vaults. Many teams also want shared instructions and skills for Claude.
Put those in an ordinary repo that everyone clones, for example `acme-workspace`,
containing `CLAUDE.md` and `.claude/skills/`. Then:

```bash
git clone https://github.com/acme/acme-workspace ~/Acme
vl folder set ~/Acme --writes acme-everyone --auto-pull
```

Everyone starts Claude in `~/Acme` for team work. The workspace stays up to date on
every sync. Protect its main branch with required reviews, so nobody can quietly
change the shared instructions.

## Obsidian

Every vault is a normal Obsidian vault. `vl` adds new vaults to Obsidian's vault
switcher; close Obsidian first, then run `vl apply`. Each person's `.obsidian/`
settings folder is kept out of git.

## Platforms

Everything works on macOS. On Linux everything works except the background job: add
a cron entry instead, for example `*/10 * * * * vl sync --background`.

## Development

```bash
uv run --group dev pytest       # unit tests
tests/e2e.sh                    # end-to-end: two fake computers, local git remotes
```

The end-to-end test runs `vl` as two users, each with a temporary home folder. It
touches nothing else on your computer. Two test-only variables stand in for GitHub:
`VAULTLINES_TEST_REMOTES=1` allows `file://` remotes, and `VAULTLINES_FAKE_AUDIENCE`
holds who can see each remote, as JSON.

## License

MIT. Basic Memory is a separate project under the AGPL license; `vl` runs it but
doesn't include it.
