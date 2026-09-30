# vaultlines

**Personal and team memory vaults for Claude Code, with access levels and git sync.**

`vl` gives Claude Code a long-term memory made of plain markdown files. You get a
**personal** vault that only you see, plus **private** and **public** vaults you
share with a team. Each folder you work in decides which vaults Claude can read and
write. Notes sync between computers through git, in the background.

Under the hood it wires together three tools you may already use:

- [Basic Memory](https://github.com/basicmachines-co/basic-memory) stores the notes
  and gives Claude fast, offline search over them.
- [Claude Code](https://claude.com/claude-code) settings and permission rules
  decide which vaults each folder can use.
- **git** syncs each vault. Any remote works: a private GitHub repo, or a bare repo
  on your own server.

Vaults are ordinary folders, so you can also open them in
[Obsidian](https://obsidian.md).

```
$ vl status
Vaults
  personal (default)  personal      ~/Vaults/personal      github:you/vault-personal   last commit 2 minutes ago
  acme-private        private:acme  ~/Vaults/acme-private  github:acme/acme-private    last commit 1 hour ago
  acme-public         public:acme   ~/Vaults/acme-public   github:acme/acme-public     last commit 5 minutes ago

Folders
  (everywhere else)       -> personal
  ~/code/acme-legal       -> acme-private  (also reads acme-public)
  ~/code/acme-app         -> acme-public

Background sync: on, every 10 min
```

## How it works

### Three levels of vault

| Level | Who has it | Example |
|---|---|---|
| `personal` | Only you | Your own notes, preferences, side projects |
| `private` | A few people on a team | Founders: legal, finances, hiring |
| `public` | The whole team | Customers, processes, decisions, how-tos |

Each vault is a folder of markdown files, a git repo, and a Basic Memory project.
Team vaults (`private` and `public`) belong to a team, such as `--team acme`.

### Folders are bound to vaults

When you start Claude in a folder, the vault bound to that folder decides what Claude
can use. Folders you haven't bound use your default personal vault.

| Folder bound to | Read and write | Read, and write only after asking you | Not available |
|---|---|---|---|
| a `personal` vault | that vault | every `public` vault | other personal vaults, `private` vaults |
| a `private` vault | that vault | its team's `public` vault | `personal`, other teams |
| a `public` vault | that vault | nothing else | `personal`, `private` |
| nothing (the default) | your default personal vault | nothing else | team vaults |

This follows the rule **"no read up, no write down"**:

- A folder never sees a vault that is more private than its own.
- Moving information into a less private vault needs your approval.

The approvals are Claude Code **ask rules**, and the blocks are **deny rules**. Both
still apply in bypass-permissions mode.

### What Claude does on its own

The [Basic Memory plugin](https://github.com/basicmachines-co/basic-memory/tree/main/plugins/claude-code)
(installed by `vl init`) does two things automatically:

- **At the start of each session,** it loads a short briefing from the folder's vault.
- **Before Claude compacts a long conversation,** it saves a checkpoint note in the
  vault's `sessions/` folder.

`sessions/` is never synced. Checkpoints stay on the computer that wrote them, so
nothing lands in a shared vault by accident. Anything else Claude saves, you asked for,
or your instructions told it to save.

### Sync

A background job (launchd on macOS) runs `vl sync` every 10 minutes. For each vault
it commits changes, pulls with rebase, and pushes. Notes use git's `union` merge, so
when two people edit the same note at the same time, both edits are kept and sync
never stops on a conflict. If sync fails anyway, you get a notification.

Repos you `vl follow` (for example a team's shared instructions repo) are pulled
too, fast-forward only.

## Install

You need **git**, **[uv](https://docs.astral.sh/uv/)** and
**[Claude Code](https://claude.com/claude-code)**. For GitHub remotes you also need
the **[GitHub CLI](https://cli.github.com)** (`gh`), signed in.

```bash
uv tool install git+https://github.com/sarink/vaultlines
vl init
```

`vl init` does the following:

1. Sets Basic Memory so it doesn't rewrite notes that other people also edit.
2. Installs the Basic Memory plugin for Claude Code.
3. Creates your personal vault at `~/Vaults/personal`, backed by a new private GitHub
   repo, `vault-personal`. Use `--personal-remote none` to keep it on this computer
   only.
4. Starts background sync.

## Quick start

**On your own:** after `vl init` you're done. Claude now has a personal memory in
every folder.

**Start a team** (for example as a founder):

```bash
vl vault create acme-public  --level public  --team acme --github acme/acme-public
vl vault create acme-private --level private --team acme --github acme/acme-private

vl bind acme-private ~/code/acme-legal     # founder work
vl bind acme-public  ~/code/acme-app       # everyday team work
```

Then give the team access to `acme/acme-public` on GitHub. Give only the founders
access to `acme/acme-private`.

**Join a team:**

```bash
vl init
vl vault join https://github.com/acme/acme-public.git --level public --team acme
vl bind acme-public ~/code/acme-app
```

**Move existing notes in:** `vl vault adopt ~/Notes --level personal` takes over a
folder you already have, and makes it a git repo if it isn't one.

## Commands

| Command | What it does |
|---|---|
| `vl init` | Set up this computer. Safe to run again. |
| `vl vault create NAME --level L [--team T]` | New vault. By default this creates a private GitHub repo `OWNER/vault-NAME`. Use `--github OWNER/REPO` for another repo, `--remote URL` for any git remote, or `--remote none`. |
| `vl vault join URL --level L [--team T]` | Clone a vault someone else created. |
| `vl vault adopt PATH --level L [--team T]` | Manage a folder that already has notes. |
| `vl vault remove NAME [--delete-files]` | Stop managing a vault. Its files stay unless you pass `--delete-files`. |
| `vl bind VAULT [FOLDER]` | Make a folder (default: the current one) use a vault. |
| `vl unbind [FOLDER]` | Go back to the default vault. |
| `vl follow PATH` / `vl unfollow PATH` | Keep a git repo pulled on every sync. |
| `vl sync [VAULT]` | Sync now. |
| `vl status` | Vaults, folders, sync. |
| `vl apply` | Rewrite Claude Code and Obsidian settings from the config. |
| `vl doctor` | Check that everything is set up. |
| `vl uninstall` | Stop background sync. Everything else stays. |

## The config file

Everything lives in `~/.config/vaultlines/config.toml`. You can edit it by hand, then
run `vl apply`.

```toml
[settings]
vaults_dir = "~/Vaults"
sync_interval = 600
bm_command = "uvx basic-memory"
github_owner = "you"
default_vault = "personal"

[vaults.personal]
path = "~/Vaults/personal"
level = "personal"
remote = "https://github.com/you/vault-personal.git"

[vaults.acme-public]
path = "~/Vaults/acme-public"
level = "public"
team = "acme"
remote = "https://github.com/acme/acme-public.git"

[bindings]
"~/code/acme-app" = "acme-public"

[follow]
repos = ["~/code/acme-workspace"]
```

## What `vl` changes on your computer

`vl` only touches things it owns, so `vl apply` can rewrite them safely:

| Where | What |
|---|---|
| Claude Code MCP servers | Servers named `vl-<vault>`. The default vault is user-level. The others are added only for the folders bound to them (local scope, stored in `~/.claude.json`, never in a repo). |
| `~/.claude/settings.json` | The `basicMemory` block, and deny rules starting with `mcp__vl-` that stop Claude from adding or deleting Basic Memory projects. |
| `<folder>/.claude/settings.local.json` | The `basicMemory` block, and the ask and deny rules starting with `mcp__vl-`. If the folder is a git repo and the file isn't ignored yet, `vl` adds it to `.git/info/exclude`. |
| Basic Memory | One project per vault. |
| Each vault | A git repo with `.gitignore` (`sessions/`, `.obsidian/`) and `.gitattributes` (`*.md merge=union`). |
| Obsidian | Adds each vault to the vault switcher, only while Obsidian is closed. |
| macOS | `~/Library/LaunchAgents/com.vaultlines.sync.plist`, logging to `~/Library/Logs/vaultlines.log`. |

Your other settings, rules and servers are left alone.

## Security model

- **The real boundary is which vaults exist on a computer.** Someone who never
  receives the `acme-private` repo can't read it. Give GitHub access with that in
  mind.
- **Folder rules prevent mistakes; they are not a sandbox.** They stop Claude from
  using the wrong vault through its tools. They can't stop a person, or a shell
  command, from copying files.
- **You are still a channel.** If you tell a public-level session something private,
  Claude can write it to the public vault. Do private work in private folders.
- **Your git host stores your notes.** With GitHub remotes, GitHub holds a copy, even
  of private repos. For full control, use `--remote` with a bare repo on a machine you
  own, such as `ssh://mini.local/~/vaults/personal.git`.
- **Leaving a team doesn't delete notes.** When someone leaves, remove their repo
  access. The copy already on their computer stays.

## A team workspace (optional)

`vl` handles vaults. Many teams also want shared instructions and skills for Claude.
Put those in an ordinary repo that everyone clones, for example `acme-workspace`,
containing `CLAUDE.md` and `.claude/skills/`. Then:

```bash
git clone https://github.com/acme/acme-workspace ~/Acme
vl bind acme-public ~/Acme
vl follow ~/Acme
```

Everyone starts Claude in `~/Acme` for team work. The workspace stays up to date
through `vl sync`. Protect its main branch with required reviews, so nobody can
quietly change the shared instructions.

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
touches nothing else on your computer.

## License

MIT. Basic Memory is a separate project under the AGPL license; `vl` runs it but
doesn't include it.
