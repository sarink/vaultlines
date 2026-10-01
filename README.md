# vaultlines

**Personal and team memory vaults for Claude Code. Git syncs them, GitHub says who
can see each one, and a Claude Code hook stops a session from copying notes to
people who couldn't see them.**

`vl` gives Claude Code a long-term memory made of plain markdown files, split into
**vaults**: one for you, and others you share with teams, clients or friends. Each
folder you work in says which vault Claude **writes** to and which others it may
**read**. While Claude works, `vl` remembers which vaults the session has read. If a
write would show those notes to someone new, `vl` asks you first.

`vl` is three things:

- **A CLI** that creates, joins and syncs vaults, and says which folders use which.
- **A background sync** (git: commit, pull, push) every 10 minutes.
- **A Claude Code hook** that checks every tool call that touches a vault.

It works with the tools you may already use:

- [Basic Memory](https://github.com/basicmachines-co/basic-memory) gives Claude
  fast, offline search over the notes. `vl` sets it up and knows how its tools
  choose a vault.
- **git and GitHub** sync each vault, and GitHub's access lists say who can see it.
- Vaults are ordinary folders, so you can also open them in
  [Obsidian](https://obsidian.md).

Anything may write files into a vault: you, Obsidian, a script, an import job. `vl`
only guards what Claude does.

## An example

Sam co-founded Acme, and has a side project.

```toml
# ~/.config/vaultlines/config.toml

[settings]
sync_interval  = 600      # sync vaults with GitHub every 10 minutes
check_interval = 86400    # ask GitHub once a day who can see each vault
on_leak        = "ask"    # a write that shows notes to new people: "ask" you, or "block" it

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

# ---------------------------------------------------------------- folders
# Which vaults Claude uses in each folder. Subfolders use the closest listed parent.

[folders."~"]                                # everything in your home folder
writes = "personal"

[folders."~/code/acme"]                      # every Acme repo
writes = "acme-founders"
reads  = ["acme-everyone"]

[folders."~/code/acme/app"]                  # everyday Acme work
writes    = "acme-everyone"
auto_pull = true                             # keep this repo pulled

[folders."~/code/side-project"]
writes = "side-project"
reads  = ["personal"]

# ---------------------------------------------------------------- plugins
[plugins.basic-memory]                       # present = on
kind = "basic-memory"
```

GitHub says who can see each vault:

| Vault | Who can see it |
|---|---|
| `personal` | sam |
| `side-project` | sam (no remote) |
| `acme-founders` | sam, lee |
| `acme-everyone` | sam, lee, ana, raj |

What happens:

| Folder | Claude does | Result | Why |
|---|---|---|---|
| `~/code/blog` | reads `acme-founders` | ❌ blocked | `"~"` applies there, and it doesn't use Acme vaults |
| `~/code/acme/api` | starts a session | saves to `acme-founders`, may read `acme-everyone` | it uses its parent `~/code/acme` |
| `~/code/acme` | reads `acme-everyone`, then writes `acme-founders` | ✅ allowed | everyone who sees `acme-founders` can see `acme-everyone` |
| `~/code/acme/app` | reads `acme-founders`… | ❌ blocked | `app` doesn't list `acme-founders` |
| `~/code/acme` | reads `acme-founders`, then writes `acme-everyone` | ⚠️ asks you | ana and raj would see it; `acme-everyone` is also a read vault here |
| `~/code/side-project` | reads `personal`, then writes `side-project` | ✅ allowed | only Sam sees `side-project` |
| anywhere | reads a web page, then writes a vault | ✅ allowed | only vault reads count (see the security model) |

Later, if Sam invites a contractor to `acme-founders` on GitHub, the daily check
notices. From then on, writing `acme-founders` after reading `acme-everyone` asks
first, because the contractor can't see `acme-everyone`.

## How it works

### Folders choose which vaults Claude uses (focus)

Each entry under `[folders]` says:

- **`writes`**: the vault Claude saves notes to in this folder.
- **`reads`** (optional): other vaults Claude may use here. Writing to them **always
  asks first**, so `reads` keeps meaning "read". This also protects vaults that
  another tool fills, like an import of Slack channels.
- **`auto_pull`** (optional, default `false`): pull this git repo on every sync.

A folder uses the **closest listed parent**. List `"~"` to cover everything in your
home folder, then list only the exceptions. A folder with no listed parent gets no
vaults at all.

**Every other vault doesn't exist for the session.** Claude is only told about this
folder's vaults, and a call to any other vault is blocked with "`X` is not used in
this folder". So a session in `~/code/blog` never fills its context with Acme notes.

### The session label keeps notes from leaking (safety)

`vl` remembers, per Claude session, **who can see everything the session has read**:

1. The label starts as *everyone*.
2. Each vault read narrows it to the people who can see that vault.
3. Before a write to vault V, `vl` finds the people who can see V but aren't in the
   label. If there are any, it asks you (or blocks, with `on_leak = "block"`):

   ```
   vl: This session read acme-founders. ana and raj would all see this in acme-everyone.
   ```

Details:

- A vault whose audience is unknown counts as *only you* when read.
- Reads are recorded before the call runs, even if the call then fails.
- Subagents share their session's label.
- `--resume` and compaction keep the label. A forked session (`--fork-session`), or a
  session `vl` has no record of, starts as *only you*: its context may hold anything.
- `/clear` starts a new session with a fresh label.
- In `claude -p`, "ask" means the call is blocked, because nobody is there to answer.

`vl sessions` shows recent sessions, their folder, their label and what they read.

### Who can see a vault

| Vault | Who can see it |
|---|---|
| No remote | Only you |
| Public GitHub repo | Everyone |
| Private repo you can push to | Its collaborators, from GitHub's API. This includes people who get access through the org's base permission, teams, and org owners. |
| Private repo you can only read | Unknown. GitHub won't list collaborators to read-only users. |

`vl check` asks GitHub and shows where writes will ask:

```
$ vl check
Who can see each vault
  vault          who can see it
  acme-everyone  ana, lee, raj, sam
  acme-founders  lee, sam
  personal       sam
  side-project   only you (no remote)

Where writes will ask
  ~:                    never
  ~/code/acme:          writes to acme-everyone always ask (it's in reads)
  ~/code/acme/app:      never
  ~/code/side-project:  writes to personal always ask (it's in reads)
```

**Once a day**, background sync asks GitHub again and updates what the hook uses. If
GitHub can't be reached, `vl` keeps the last answer and tries again at the next sync.

### How the hook sees which vault a call uses

The hook is `vl hook`, installed in `~/.claude/settings.json` for `SessionStart`,
`UserPromptSubmit`, and these tools only: `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Grep`,
`Glob`, `Bash` and Basic Memory's. It reads a small file that `vl` writes
(`~/.local/state/vaultlines/runtime.json`), never the config or GitHub, and adds about
50 ms to each of those calls.

- **File tools:** the path in the call (with `~`, relative paths and symlinks
  resolved). `Read`, `Grep` and `Glob` are reads; the others are writes. A `Grep` or
  `Glob` over a folder that holds several vaults reads each of them, and is blocked if
  any is outside this folder's vaults ("search a narrower folder").
- **Bash:** a vault path in the command (absolute, `~/…`, `$HOME/…`, or relative to
  the current folder) counts as a read **and** a write of that vault. This is best
  effort: a command can always build a path the scan can't see.
- **Basic Memory:** there is one Basic Memory server for all vaults, and each vault is
  a project with the vault's name. The `project` argument names the vault. If it's
  missing, `vl` fills in the folder's `writes` vault. A `memory://` link whose first
  part is another project counts too. `vl` blocks what it can't check: `project_id`,
  `workspace`, `search_all_projects`, `recent_activity` without a project, the
  `search` and `fetch` tools, project management, and any argument it doesn't know.
- **Session start:** Claude gets one line saying where to save notes and what else it
  may read.

If the hook itself fails, calls that may touch a vault are blocked with
"vl hook error: run `vl doctor`". Other calls are never blocked.

### Claude can't quietly loosen vl

Claude can run `vl` and edit files like anything else. So an over-helpful Claude (or
instructions hidden in a note, an email or a web page) could try to get around a block
by changing vl itself. The hook watches for that:

- **Blocked:** writing to vl's session records or `runtime.json`. Only vl writes those.
- **Asks first:**
  - `vl init`, `apply`, `uninstall`, `folder` or `vault` run by Claude;
  - edits to vl's config;
  - Bash commands that mention vl's config or state folders;
  - settings edits that remove vl's hooks or turn on `disableAllHooks`.
- **Unless you asked:** if your latest message mentions `vl` or vaultlines, those
  changes go through without a question. The hook learns this from what you type (on
  `UserPromptSubmit`), so Claude can't set it itself.

The leak checks above always apply, whatever you asked.

### What the Basic Memory plugin does on its own

The [Basic Memory plugin](https://github.com/basicmachines-co/basic-memory/tree/main/plugins/claude-code)
(installed by `vl init`) does two things by itself, outside Claude's tool calls:

- **At the start of each session,** it loads a short briefing from a vault.
- **Before Claude compacts a long conversation,** it saves a checkpoint note in that
  vault's `sessions/` folder.

`vl` points it at each listed folder's `writes` vault, and counts the briefing as a
read of that vault. The plugin uses the nearest `.claude/settings*.json`, so in a
subfolder that has its own settings file it falls back to your user-level vault; the
hook counts whichever vault the plugin actually uses.

`sessions/` is never synced. Checkpoints stay on the computer that wrote them.

### Sync

A background job (launchd on macOS) runs `vl sync` every 10 minutes. For each vault
it commits changes, pulls with rebase, and pushes. Notes use git's `union` merge, so
when two people edit the same note at the same time, both edits are kept and sync
never stops on a conflict. If sync fails anyway, you get a notification.

Folders with `auto_pull = true` are pulled too, fast-forward only. `vl` never pushes
them. If the pull can't fast-forward, it's skipped and noted in the log.

Sync also deletes session records older than 30 days.

## Install

You need **git**, **[uv](https://docs.astral.sh/uv/)**,
**[Claude Code](https://claude.com/claude-code)** and the
**[GitHub CLI](https://cli.github.com)** (`gh`), signed in with `gh auth login`.

```bash
uv tool install git+https://github.com/sarink/vaultlines
vl init
```

`vl init` does the following:

1. Sets up Basic Memory (so it doesn't rewrite notes other people also edit), its one
   server, and its Claude Code plugin. Use `--no-basic-memory` to skip this.
2. Creates your personal vault at `~/Vaults/personal`, backed by a new private GitHub
   repo, `vault-personal`. Use `--local` to keep it on this computer only (then you
   don't need `gh`).
3. Lists `"~"`, writing to `personal`.
4. Installs the Claude Code hooks and starts background sync.

## Quick start

**On your own:** after `vl init` you're done. Claude has a personal memory in every
folder in your home folder.

**Start a team** (for example as a founder):

```bash
vl vault create acme-everyone --github acme/acme-everyone
vl vault create acme-founders --github acme/acme-founders

vl folder set ~/code/acme     --writes acme-founders --reads acme-everyone
vl folder set ~/code/acme/app --writes acme-everyone
```

Then give the team access to `acme/acme-everyone` on GitHub, and give only the
founders access to `acme/acme-founders`.

**Join a team:**

```bash
vl init
vl vault join https://github.com/acme/acme-everyone.git
vl folder set ~/code/acme/app --writes acme-everyone
```

**Move existing notes in:** `vl vault adopt ~/Notes` takes over a folder you already
have, and makes it a git repo if it isn't one. Add `--github OWNER/REPO` to also
create a private GitHub repo for it.

## Commands

| Command | What it does |
|---|---|
| `vl init [--local] [--no-basic-memory]` | Set up this computer. Safe to run again. |
| `vl vault create NAME [--github OWNER/REPO \| --local]` | New vault. By default this creates a private GitHub repo `YOU/vault-NAME`. |
| `vl vault join URL [--name NAME]` | Clone a vault from GitHub. |
| `vl vault adopt PATH [--github OWNER/REPO]` | Manage a folder that already has notes. |
| `vl vault remove NAME [--delete-files]` | Stop managing a vault. Its files stay unless you pass `--delete-files`. Folders must stop using it first. |
| `vl folder set PATH --writes V [--reads A,B] [--auto-pull]` | Set a folder's vaults. It covers subfolders too. Replaces the folder's entry. |
| `vl folder unset PATH` | Remove a folder's entry, so it uses its closest listed parent. |
| `vl check` | Ask GitHub who can see each vault, and show where writes will ask. |
| `vl sync [VAULT]` | Sync now. |
| `vl status` | Vaults and who can see them, folders, hooks, sync. |
| `vl sessions` | Recent Claude sessions: folder, label, vaults read. |
| `vl apply` | Set up git, Basic Memory, the plugin, the hooks and Obsidian from the config. |
| `vl doctor` | Check that everything is set up. |
| `vl uninstall` | Remove the hooks and stop background sync. Everything else stays. |

## The config file

Everything lives in `~/.config/vaultlines/config.toml`, one per computer. You can
edit it by hand, then run `vl apply`.

- Every folder needs `writes`. `reads` is optional.
- Vault names must exist, and `writes` can't also be in `reads`.
- Two entries can't be the same folder. There is no `"*"`: list `"~"` instead.
- `remote` must be `https://github.com/OWNER/REPO` (with or without `.git`). Leave
  it out for a vault that stays on this computer.
- `on_leak` is `"ask"` (default) or `"block"`.
- `[plugins.NAME]` turns a plugin on. Every plugin needs `kind`; the only built-in
  kind so far is `"basic-memory"`, which takes an optional `command` (default
  `uvx basic-memory`). Plugins only tell `vl` which vaults a tool call touches;
  `vl` decides what's allowed.

Mistakes are reported with the file and key, for example
`~/.config/vaultlines/config.toml: folders."~/code/app".reads: no vault named 'acme'`.

Optional settings: `vaults_dir` (default `~/Vaults`). Audiences from GitHub are cached
in `~/.config/vaultlines/state.json`.

## What `vl` changes on your computer

`vl` only touches things it owns, so `vl apply` can rewrite them safely:

| Where | What |
|---|---|
| `~/.claude/settings.json` | Hooks whose command is `vl hook` (`SessionStart`, `UserPromptSubmit`, `PreToolUse`), and the `basicMemory` block for the folder that covers your home folder. |
| `<folder>/.claude/settings.local.json` | The `basicMemory` block. If the folder is a git repo and the file isn't ignored yet, `vl` adds it to `.git/info/exclude`. |
| Claude Code MCP servers | One user-level server named `basic-memory`. |
| Basic Memory | One project per vault, with the vault's name. |
| `~/.local/state/vaultlines/` | `runtime.json` (what the hook reads) and `sessions/` (one small file per session). |
| Each vault | A git repo with `.gitignore` (`sessions/`, `.obsidian/`) and `.gitattributes` (`*.md merge=union`). |
| `auto_pull` folders | `git pull --ff-only` on every sync. Nothing else. |
| Obsidian | Adds each vault to the vault switcher, only while Obsidian is closed. |
| macOS | `~/Library/LaunchAgents/com.vaultlines.sync.plist`, logging to `~/Library/Logs/vaultlines.log`. |

It also removes what vaultlines 0.2 added: `vl-*` servers and `mcp__vl-*` rules. Your
other settings, hooks and servers are left alone.

## Security model

- **The real boundary is which vaults exist on a computer.** Someone who never gets
  access to the `acme-founders` repo can't read it. Give GitHub access with that in
  mind.
- **The hook only guards Claude Code.** Other AI tools (Cursor, Claude Desktop) don't
  run it. People, scripts and Obsidian can copy files freely.
- **Only vault reads narrow the label.** Text Claude gets from other tools (email,
  Slack, web pages) counts as safe, so it can reach a shared vault without a question.
  This is a known gap, by choice.
- **Only vault writes are checked.** In a repo, Claude also writes code, docs and
  commit messages, and it could copy a note into them. That goes through your normal
  diff and pull request review.
- **Bash is best effort.** A command that builds a vault path the scan can't see gets
  through.
- **A repo can switch hooks off.** A `.claude/settings.json` with
  `"disableAllHooks": true` turns off every hook, `vl`'s included. `vl` can't prevent
  this. `vl check`, `vl status`, `vl doctor` and the daily check look for it in your
  listed folders (up to their git repo root) and warn you. Folders you never listed
  aren't checked.
- **You are still a channel.** If you tell a session something private, the label
  doesn't know, and Claude can write it to a shared vault.
- **GitHub's list is the whole list, with limits.** Collaborators include org base
  permissions, teams and owners. Deploy keys and GitHub Apps with access to a repo
  aren't people and aren't listed. Invitations count once they're accepted, at the
  next daily check.
- **`vl` checks that each vault's git remote matches the config.** If they don't
  match, the vault's audience is unknown, so reading it counts as *only you*.
- **Basic Memory projects `vl` doesn't know are blocked**, and so are new ways to pick
  a project until the plugin learns them. A project added by hand after the last
  `vl apply` isn't known to the hook until the next one.
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
uv run --group dev pytest       # unit tests, plus `vl hook` run on recorded events
tests/e2e.sh                    # end-to-end: two fake computers, local git remotes
```

The end-to-end test runs `vl` as two users, each with a temporary home folder. It
touches nothing else on your computer. Two test-only variables stand in for GitHub:
`VAULTLINES_TEST_REMOTES=1` allows `file://` remotes, and `VAULTLINES_FAKE_AUDIENCE`
holds who can see each remote, as JSON.

## License

MIT. Basic Memory is a separate project under the AGPL license; `vl` runs it but
doesn't include it.
