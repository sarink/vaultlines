"""The Google Drive source: one markdown note per Drive file, originals fetched on demand.

A Drive vault is a GitHub repo whose vault.toml has a [source] table:

    [source]
    kind                 = "gdrive"
    shared_drive         = "0AHF8p0HI9kM1Uk9PVA"   # the shared drive's ID
    shared_drive_name    = "Mixim HQ"
    folder               = ""                      # the whole drive, or a folder in it
    max_size             = "50M"                   # bigger files get a note without text
    google_client_id     = "1234-abc.apps.googleusercontent.com"
    google_client_secret = "GOCSPX-…"              # a desktop app's; Google doesn't treat it as secret

A GitHub Action in the repo fills it (`vl gdrive run`, every hour), signed in as a bot
account with read-only access. Each run lists Drive, downloads only new and changed files
to a temporary folder, turns them into text with markitdown, writes one note per file,
commits and pushes. The vault holds only notes. Everyone else only pulls it, and the hook
makes it read-only in sessions.

Each note's frontmatter points to its original, which `vl gdrive fetch OWNER/REPO PATH`
downloads with your own read-only Google sign-in, when Claude needs it.

`vl hook` imports this module, so it only imports the standard library at the top.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

NAME = "Google Drive"
SOURCE = "gdrive"  # the `source` key of every note this source writes
KEYS = {"kind", "shared_drive", "shared_drive_name", "folder", "max_size", "google_client_id", "google_client_secret"}
# rclone exports each Google type to the first of these it supports: Docs to .md,
# Sheets to .xlsx, Slides and Drawings to .pdf. Forms and others are left out.
EXPORT_FLAGS = ["--drive-export-formats", "md,xlsx,pdf", "--drive-skip-shortcuts"]
MAX_SIZE = "50M"  # bigger files get a note without text
MARKITDOWN = "0.1.8"
CONVERTER = f"markitdown {MARKITDOWN}"
MAX_TEXT = 200_000  # bytes of text in a note
BATCH = 100  # files downloaded and converted at once
RCLONE = "v1.75.0"  # installed by the Action
WORKFLOW_FILE = "vl-gdrive.yml"
COMMIT = "Update from Google Drive"

# Frontmatter keys vl writes, in order. Any other key is kept as it is.
OURS = ("title", "type", "source", "id", "path", "url", "modified", "md5", "mime", "converter", "text", "fetch")
MARKITDOWN_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown", "text/csv"}
TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".csv"}


# ---------------------------------------------------------------- the [source] table

def _test_remotes() -> bool:
    return os.environ.get("VAULTLINES_TEST_REMOTES") == "1"


def parse_size(text: str) -> int:
    """rclone's sizes: 100, 1K, 50M, 2G (powers of 1024)."""
    m = re.fullmatch(r"(\d+)([KMGT]?)", text.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(text)
    return int(m.group(1)) * 1024 ** " KMGT".index(m.group(2).upper() or " ")


def _bad_path(path: str) -> bool:
    return path.startswith(("/", "-", "~")) or any(c in path for c in "\n\r\0") or ".." in path.split("/")


def validate_source(source: dict) -> list[str]:
    """Problems with a vault's [source] table, as "key: problem"."""
    problems = [f"{key}: unknown key. Allowed: {', '.join(sorted(KEYS))}" for key in source if key not in KEYS]
    drive = source.get("shared_drive", "")
    if not isinstance(drive, str) or not (re.fullmatch(r"[A-Za-z0-9_-]*", drive)
                                          or (_test_remotes() and drive.startswith("/"))):
        problems.append("shared_drive: should be a shared drive's ID, or \"\" for My Drive")
    folder = source.get("folder", "")
    if not isinstance(folder, str) or (folder and _bad_path(folder.strip("/") if folder.endswith("/") else folder)):
        problems.append("folder: should be a folder in the drive, like \"Finance/2024\", or \"\" for all of it")
    max_size = source.get("max_size", MAX_SIZE)
    try:
        parse_size(max_size if isinstance(max_size, str) else "")
    except ValueError:
        problems.append('max_size: should be a size, like "50M"')
    for key in ("google_client_id", "google_client_secret"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            problems.append(f"{key}: missing. `vl gdrive add` writes it")
    return problems


def _source(vault) -> dict:
    """A vault's [source] table, checked."""
    from ..util import VlError

    source = vault.source or {}
    if source.get("kind") != SOURCE:
        raise VlError(f"{vault.id} isn't filled from Google Drive.")
    problems = validate_source(source)
    if problems:
        raise VlError(f"{vault.id}'s vault.toml has problems in [source]: " + "; ".join(problems))
    return source


def remote_path(source: dict) -> str:
    """The rclone path of the drive (or its folder). In tests, shared_drive may be a local folder."""
    folder = (source.get("folder") or "").strip("/")
    drive = source.get("shared_drive") or ""
    if _test_remotes() and drive.startswith("/"):
        return os.path.join(drive, folder) if folder else drive
    return f"gdrive:{folder}"


def rclone_config(source: dict, access: str, refresh: str) -> str:
    """A temporary rclone.conf for one run: the bot's read-only sign-in and the drive."""
    import datetime as dt

    expiry = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
    token = json.dumps({"access_token": access, "token_type": "Bearer", "refresh_token": refresh, "expiry": expiry})
    lines = ["[gdrive]", "type = drive", "scope = drive.readonly",
             f"client_id = {source['google_client_id']}", f"client_secret = {source['google_client_secret']}",
             f"token = {token}"]
    if source.get("shared_drive"):
        lines.append(f"team_drive = {source['shared_drive']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- rclone

def _env() -> dict[str, str]:
    """The environment for rclone, without RCLONE_* variables: they can change the remote
    or its credentials."""
    return {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}


def _binary() -> str:
    import shutil

    from ..util import VlError

    found = shutil.which("rclone")
    if not found:
        raise VlError("rclone isn't installed (see https://rclone.org/install/).")
    return found


def _errors(stderr: str) -> str:
    # rclone ends with a summary, like "NOTICE: Failed to copy: directory not found"
    lines = dict.fromkeys(re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ", "", line)
                          for line in stderr.strip().splitlines()
                          if "Attempt " not in line)  # rclone retries, repeating the same errors
    return "\n".join(list(lines)[-8:])


def _rclone(conf: str | None, *args: str, check: bool = True):
    """Run rclone with vl's own config file (or none, for a local folder) and no RCLONE_* variables."""
    import subprocess

    from ..util import VlError

    cmd = [_binary(), "--config", conf or os.devnull, *args]
    result = subprocess.run(cmd, env=_env(), text=True, capture_output=True, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise VlError(f"rclone {args[0]} failed:\n{_errors(result.stderr)}")
    return result


# ---------------------------------------------------------------- planning

@dataclass
class File:
    """A file in Drive, from the listing."""
    id: str
    path: str
    size: int
    mime: str
    modified: str
    md5: str = ""

    @property
    def key(self) -> str:
        return self.id or f"path:{self.path}"

    @classmethod
    def from_json(cls, item: dict) -> File:
        return cls(id=item.get("ID") or "", path=item["Path"], size=int(item.get("Size", -1)),
                   mime=item.get("MimeType") or "", modified=item.get("ModTime") or "",
                   md5=(item.get("Hashes") or {}).get("md5") or "")


@dataclass
class Note:
    """A note this source wrote. `file` is its path in the vault."""
    file: str
    meta: dict
    extra: list[str]

    @property
    def key(self) -> str:
        return self.meta.get("id") or f"path:{self.meta.get('path')}"


@dataclass
class Change:
    kind: str  # "add", "update", "move" or "delete"
    file: File | None
    note: Note | None
    dest: str | None  # the note's new path in the vault


def _skipped(path: str) -> bool:
    """Drive paths vl never writes a note for: unsafe, or kept out of git by vl."""
    parts = path.split("/")
    return (path.startswith("/") or any(c in path for c in "\n\r\0")
            or any(p in ("", ".", "..", ".git") for p in parts)
            or parts[0] in ("sessions", ".obsidian") or parts[-1] == ".DS_Store")


def _fold(path: str) -> str:
    """Paths that are the same file on macOS (case and Unicode form don't matter)."""
    return unicodedata.normalize("NFC", path).casefold()


def _tag(f: File) -> str:
    import hashlib

    return f.id[:6] if f.id else hashlib.sha1(f.path.encode()).hexdigest()[:6]


def note_paths(files: list[File], taken: set[str] = frozenset()) -> dict[str, str]:
    """Each file's note: Drive's path, plus .md unless it ends in .md. When two files
    would share a note, the later gets part of its ID added. `taken`: other files."""
    used = {_fold(t) for t in taken}
    out = {}
    for f in sorted(files, key=lambda f: (f.path, f.id)):
        if _skipped(f.path) or f.key in out:
            continue
        base = f.path[:-3] if f.path.lower().endswith(".md") else f.path
        dest = base + ".md"
        for tag in (_tag(f), f.id or f.path):
            if _fold(dest) not in used:
                break
            dest = f"{base} ({tag}).md"
        used.add(_fold(dest))
        out[f.key] = dest
    return out


def _stale(f: File, n: Note) -> bool:
    return (n.meta.get("modified") != f.modified or (n.meta.get("md5") or "") != f.md5
            or n.meta.get("converter") != CONVERTER)


def changes(files: list[File], notes: list[Note], rebuild: bool = False, taken: set[str] = frozenset()) -> list[Change]:
    """What to do to make the notes match the listing. Pure."""
    paths = note_paths(files, taken)
    plan = []
    by_key: dict[str, Note] = {}
    for n in sorted(notes, key=lambda n: (n.file != paths.get(n.key), n.file)):  # the note in the right place wins
        if n.key not in paths or n.key in by_key:
            plan.append(Change("delete", None, n, None))
        else:
            by_key[n.key] = n
    seen = set()
    for f in sorted(files, key=lambda f: (f.path, f.id)):
        dest = paths.get(f.key)
        if dest is None or f.key in seen:
            continue
        seen.add(f.key)
        n = by_key.get(f.key)
        if n is None:
            plan.append(Change("add", f, None, dest))
        elif rebuild or _stale(f, n):
            plan.append(Change("update", f, n, dest))
        elif n.file != dest or n.meta.get("path") != f.path:
            plan.append(Change("move", f, n, dest))
    return plan


# ---------------------------------------------------------------- notes

def _value(text: str) -> str:
    text = text.strip()
    if text.startswith('"'):
        try:
            return str(json.loads(text))
        except ValueError:
            return text.strip('"')
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def parse_note(text: str) -> tuple[dict, list[str], str]:
    """(vl's keys, every other frontmatter line as it is, body)."""
    if not text.startswith("---\n"):
        return {}, [], text
    end = text.find("\n---\n", 3)
    if end < 0:
        if not text.endswith("\n---"):
            return {}, [], text
        end = len(text) - 4
    meta, extra = {}, []
    ours = False  # the line above started one of vl's keys
    for line in text[4:end].split("\n"):
        m = re.match(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        if m:
            ours = m.group(1) in OURS and bool(m.group(2))
            if ours:
                meta[m.group(1)] = _value(m.group(2))
                continue
        if line.strip() and not ours:
            extra.append(line)
    body = text[end + 5:]
    return meta, extra, body.removeprefix("\n")


def render_note(meta: dict, extra: list[str], body: str) -> str:
    """Values are JSON strings, which YAML reads too."""
    lines = [f"{k}: {json.dumps(str(meta[k]), ensure_ascii=False)}" for k in OURS if meta.get(k)]
    return "---\n" + "\n".join(lines + list(extra)) + "\n---\n\n" + body


def read_notes(root: Path, source: str = SOURCE) -> list[Note]:
    """Every note in the vault this source wrote. Others are never touched."""
    out = []
    for rel in _markdown(root):
        try:
            meta, extra, _ = parse_note((root / rel).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if meta.get("source") == source and meta.get("path"):
            out.append(Note(rel, meta, extra))
    return out


def _markdown(root: Path) -> list[str]:
    """Every .md file in a vault, apart from what vl keeps out of git."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirs[:] = [d for d in dirs if d != ".git" and not (rel_dir == "." and d in ("sessions", ".obsidian"))]
        for name in files:
            if name.endswith(".md"):
                out.append(os.path.normpath(os.path.join(rel_dir, name)))
    return sorted(out)


def kind_of(mime: str, path: str) -> str | None:
    """How a file becomes text: "text" (used as it is), a markitdown extension, or None."""
    mime = mime.split(";")[0].strip().lower()
    ext = os.path.splitext(path)[1].lower()
    if mime in MARKITDOWN_TYPES:
        return MARKITDOWN_TYPES[mime]
    if mime in TEXT_TYPES or ext in TEXT_EXTENSIONS:
        return "text"
    return ext if ext in MARKITDOWN_TYPES.values() else None


def fetch_command(vault_id: str, path: str) -> str:
    """The command that fetches a file, quoted for the shell."""
    quoted = re.sub(r'([\\"$`])', r"\\\1", path)
    return f'vl gdrive fetch {vault_id} "{quoted}"'


def cut(text: str, fetch: str) -> tuple[str, str]:
    """A note's body and its `text` status: full, truncated or no text."""
    if not text.strip():
        return "", "no text"
    data = text.encode()
    if len(data) <= MAX_TEXT:
        return (text if text.endswith("\n") else text + "\n"), "full"
    head = data[:MAX_TEXT].decode("utf-8", "ignore")
    head = head[: head.rfind("\n")] if head.rfind("\n") > MAX_TEXT // 2 else head
    return (head.rstrip("\n") + f"\n\nThis note is cut short at {MAX_TEXT // 1000} KB. "
            f"For the whole file, run `{fetch}`.\n"), "truncated"


def _title(path: str) -> str:
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", path.rsplit("/", 1)[-1])


def _meta(f: File, source: str, vault: str, text: str) -> dict:
    return {"title": _title(f.path), "type": "drive-file", "source": source, "id": f.id, "path": f.path,
            "url": f"https://drive.google.com/open?id={f.id}" if f.id else "", "modified": f.modified,
            "md5": f.md5, "mime": f.mime, "converter": CONVERTER, "text": text,
            "fetch": fetch_command(vault, f.path)}


def _reason(status: str, fetch: str, max_size: str, f: File) -> str:
    if status.startswith("failed: "):
        why = f"vl couldn't convert this file ({status[len('failed: '):]})."
    elif status == "duplicate name":
        return (f"Another file in this Drive folder has the same name, so vl can't tell them apart. "
                f"Open it in Drive: {f.id and 'https://drive.google.com/open?id=' + f.id or f.path}\n")
    else:
        why = {"no text": "vl found no text in this file (it may be a scan).",
               "too big": f"This file is bigger than max_size ({max_size}), so vl didn't convert it.",
               "not convertible": "vl can't turn this kind of file into text."}[status]
    return f"{why} For the original, run `{fetch}`.\n"


# ---------------------------------------------------------------- a run

def _list(remote: str, conf: str | None) -> list[File]:
    out = _rclone(conf, "lsjson", "-R", "--files-only", "--hash-type", "md5", "--fast-list", *EXPORT_FLAGS,
                  remote).stdout
    return [File.from_json(item) for item in json.loads(out or "[]")]


def _download(remote: str, conf: str | None, paths: list[str], dest: str) -> str:
    """Copy these files into `dest`, in one rclone call. Returns rclone's errors, if any."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8") as listing:
        listing.write("".join(p + "\n" for p in paths))
        listing.flush()
        result = _rclone(conf, "copy", remote, dest, "--files-from-raw", listing.name, *EXPORT_FLAGS, check=False)
    return _errors(result.stderr) if result.returncode != 0 else ""


CONVERT_SCRIPT = r"""
import json, sys
from markitdown import StreamInfo
from markitdown.converters import DocxConverter, PdfConverter, PptxConverter, XlsxConverter
# Only the converter for the file's type: markitdown on its own falls back to reading
# a broken PDF as plain text.
converters = {".pdf": PdfConverter(), ".docx": DocxConverter(), ".xlsx": XlsxConverter(), ".pptx": PptxConverter()}
job = json.load(sys.stdin)
out = []
for f in job["files"]:
    try:
        with open(f["file"], "rb") as fh:
            text = converters[f["ext"]].convert(fh, StreamInfo(extension=f["ext"])).markdown or ""
        out.append({"text": text.replace("\f", "\n")})
    except Exception as e:
        out.append({"error": " ".join(f"{type(e).__name__}: {e}".split())[:200]})
with open(job["out"], "w", encoding="utf-8") as fh:
    json.dump(out, fh)
"""


def _convert(jobs: list[tuple[str, str]], work: str) -> list[dict]:
    """Turn files into text with markitdown, in one batch: [{"text"} or {"error"}] per file."""
    import shutil
    import subprocess

    from ..util import VlError

    uv = shutil.which("uv")
    if not uv:
        raise VlError("uv isn't installed, and vl runs markitdown with it. Install it with `brew install uv`.")
    out = os.path.join(work, "converted.json")
    job = {"files": [{"file": path, "ext": ext} for path, ext in jobs], "out": out}
    result = subprocess.run([uv, "run", "--quiet", "--no-project", "--with", f"markitdown[pdf,docx,xlsx,pptx]=={MARKITDOWN}",
                             "python", "-c", CONVERT_SCRIPT], input=json.dumps(job), text=True, capture_output=True)
    if result.returncode != 0 or not os.path.exists(out):
        raise VlError("markitdown failed:\n" + "\n".join(result.stderr.strip().splitlines()[-8:]))
    with open(out, encoding="utf-8") as fh:
        return json.load(fh)


def _batches(items: list, size: int = BATCH) -> list[list]:
    """Groups of files to download together. Two paths that are the same file on macOS
    go in different groups, so one download doesn't replace the other."""
    groups: list[tuple[list, set]] = []
    for item in items:
        key = _fold(item[0].file.path)
        group = next((g for g in groups if len(g[0]) < size and key not in g[1]), None)
        if group is None:
            group = ([], set())
            groups.append(group)
        group[0].append(item)
        group[1].add(key)
    return [g[0] for g in groups]


def _write(root: Path, change: Change, meta: dict, body: str) -> None:
    """Write a note at its place, keeping keys Basic Memory added."""
    dest = root / change.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_note(meta, change.note.extra if change.note else [], body), encoding="utf-8")


def _remove_empty_folders(root: Path) -> None:
    for dirpath, dirs, files in os.walk(root, topdown=False):
        rel = os.path.relpath(dirpath, root)
        if rel == "." or rel.split(os.sep)[0] in (".git", "sessions", ".obsidian"):
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass  # not empty


def run(root: Path, settings: dict, vault_id: str, remote: str, conf: str | None, rebuild: bool = False) -> str:
    """Make the notes in `root` match the Drive folder `remote` (an rclone path; `conf` its
    rclone.conf). Returns a status like "3 new, 1 changed, 0 moved, 0 deleted"."""
    import tempfile
    from collections import Counter

    from ..util import VlError

    source = SOURCE
    max_size = settings.get("max_size") or MAX_SIZE
    files = _list(remote, conf)
    notes = read_notes(root, source)
    taken = set(_markdown(root)) - {n.file for n in notes}
    plan = changes(files, notes, rebuild, taken)
    count = Counter(c.kind for c in plan)

    # Notes that move are read and taken away before any is written, so two files that
    # swap names don't overwrite each other's notes.
    moving = {}
    for c in plan:
        if c.kind == "move":
            moving[id(c)] = parse_note((root / c.note.file).read_text(encoding="utf-8"))
        if c.kind == "delete" or (c.kind in ("move", "update") and c.note.file != c.dest):
            (root / c.note.file).unlink(missing_ok=True)
    for c in plan:
        if c.kind == "move":
            meta, c.note.extra, body = moving[id(c)]
            fresh = _meta(c.file, source, vault_id, meta.get("text", ""))
            meta.update({k: fresh[k] for k in ("title", "id", "path", "url", "fetch")})
            _write(root, c, meta, body)

    def note(c: Change, status: str = "", text: str | None = None) -> None:
        """Write a file's note: from its text, or with only a reason for having none."""
        fetch = fetch_command(vault_id, c.file.path)
        if text is not None:
            body, status = cut(text, fetch)
        if text is None or status == "no text":
            body = _reason(status, fetch, max_size, c.file)
        _write(root, c, _meta(c.file, source, vault_id, status), body)

    duplicates = {p for p, n in Counter(f.path for f in files).items() if n > 1}
    todo = []
    for c in plan:
        if c.kind not in ("add", "update"):
            continue
        kind = kind_of(c.file.mime, c.file.path)
        if c.file.path in duplicates:
            note(c, "duplicate name")
        elif kind is None:
            note(c, "not convertible")
        elif c.file.size > parse_size(max_size):
            note(c, "too big")
        else:
            todo.append((c, kind))

    missing, problems = [], []
    for batch in _batches(todo):
        with tempfile.TemporaryDirectory(prefix="vl-drive-") as work:
            got = os.path.join(work, "files")
            error = _download(remote, conf, [c.file.path for c, _ in batch], got)
            if error:
                problems.append(error)
            jobs = []
            for c, kind in batch:
                local = os.path.join(got, c.file.path)
                if not os.path.isfile(local):
                    missing.append(c.file.path)  # no note yet, so the next run tries again
                elif kind == "text":
                    with open(local, "rb") as fh:
                        note(c, text=fh.read().decode("utf-8", "replace").lstrip("\ufeff"))
                else:
                    jobs.append((c, local, kind))
            if jobs:
                for (c, _, _), result in zip(jobs, _convert([(local, kind) for _, local, kind in jobs], work)):
                    if "error" in result:
                        note(c, f"failed: {result['error']}")
                    else:
                        note(c, text=result["text"])

    _remove_empty_folders(root)
    status = f"{count['add']} new, {count['update']} changed, {count['move']} moved, {count['delete']} deleted"
    if missing:
        detail = f"{problems[-1]}\n" if problems else ""  # `vl status` shows the last line
        raise VlError(f"{detail}{status}, but rclone couldn't download {len(missing)} file(s), like {missing[0]!r}. "
                      "They're tried again next run.")
    return status


# ---------------------------------------------------------------- the hook's side

def source_briefing(short: str, vault_id: str) -> str:
    return (f"`{short}` holds notes converted from Google Drive; for an original, run "
            f"`vl gdrive fetch {vault_id} \"<path from the note's frontmatter>\"`.")


# ---------------------------------------------------------------- the GitHub Action

WORKFLOW = """\
# Written by `vl gdrive add`. It fills this vault from Google Drive.
name: vl gdrive
on:
  schedule: [{ cron: "17 * * * *" }]        # hourly; edit to change
  workflow_dispatch: { inputs: { rebuild: { type: boolean, default: false } } }
concurrency: { group: vl-gdrive }            # one run at a time
permissions: { contents: write }
jobs:
  fill:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: curl -fsSL https://rclone.org/install.sh | sudo bash -s __RCLONE__
      - run: uvx --from "git+https://github.com/sarink/vaultlines@v__VERSION__" vl gdrive run ${{ inputs.rebuild && '--rebuild' || '' }}
        env:
          VL_GDRIVE_TOKEN: ${{ secrets.VL_GDRIVE_TOKEN }}   # read-only refresh token of the bot account
"""


def workflow() -> str:
    from .. import __version__

    return WORKFLOW.replace("__RCLONE__", RCLONE).replace("__VERSION__", __version__)


def _vault_id_here(root: Path, given: str | None) -> str:
    from ..util import VlError
    from ..vaults import git_origin, remote_id

    found = (given or os.environ.get("GITHUB_REPOSITORY") or remote_id(git_origin(root)) or "").strip().lower()
    if not found or "/" not in found:
        raise VlError("Can't tell which vault this is. Run it in the vault's clone, or pass --vault OWNER/REPO.")
    return found


def cmd_run(args) -> None:
    """The Action: convert, commit, push. Runs in the vault's checkout."""
    import tempfile

    from .. import gitsync, google
    from ..util import VlError, say
    from ..vaults import VAULT_FILE, parse_vault_toml

    root = Path(args.path or ".").resolve()
    info, problems = parse_vault_toml((root / VAULT_FILE).read_text() if (root / VAULT_FILE).exists() else "")
    source = info.source or {}
    if source.get("kind") != SOURCE:
        raise VlError(f"{root / VAULT_FILE} has no [source] with kind = \"gdrive\".")
    problems = validate_source(source)
    if problems:
        raise VlError("vault.toml [source]: " + "; ".join(problems))
    vault_id = _vault_id_here(root, args.vault)
    if not gitsync.git(root, "config", "user.email", check=False).stdout.strip():
        gitsync.git(root, "config", "user.name", "github-actions[bot]")
        gitsync.git(root, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    remote = remote_path(source)
    with tempfile.TemporaryDirectory(prefix="vl-gdrive-") as work:
        conf = None
        if not (_test_remotes() and (source.get("shared_drive") or "").startswith("/")):
            token = os.environ.get("VL_GDRIVE_TOKEN", "").strip()
            if not token:
                raise VlError("VL_GDRIVE_TOKEN isn't set. `vl gdrive add` puts it in the repo's Actions secrets.")
            access = google.access_token(source["google_client_id"], source["google_client_secret"], token)
            google.check_read_only(access)
            conf = os.path.join(work, "rclone.conf")
            fd = os.open(conf, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(rclone_config(source, access, token))
        try:
            status = run(root, source, vault_id, remote, conf, rebuild=args.rebuild)
        except VlError:
            if gitsync.commit(root, "Partial update from Google Drive"):  # what did arrive is kept
                _push(root)
            raise
    if gitsync.commit(root, COMMIT):
        _push(root)
        say(status)
    else:
        say(f"no changes ({status})")


def _push(root: Path) -> None:
    from .. import gitsync
    from ..util import VlError

    if gitsync.remote_url(root) and gitsync.git(root, "push", "-q", "origin", "HEAD", check=False).returncode != 0:
        raise VlError("couldn't push the notes")


# ---------------------------------------------------------------- vl gdrive add (admins)

def cmd_add(args) -> None:
    from .. import config, github, gitsync, google, vaults
    from ..cli import _apply, _discovery, _record
    from ..util import VlError, contract, say

    vault_id = args.vault.strip().lower()
    if not vaults.valid_id(vault_id) or not vault_id.split("/", 1)[1].startswith(vaults.PREFIX):
        raise VlError("Give OWNER/vault-NAME, like mixim-ai/vault-hq.")
    owner = vault_id.split("/")[0]
    cfg = config.load()
    if owner not in cfg.owners:
        raise VlError(f"You haven't joined {owner}. Run `vl org join {owner}` first.")
    if vault_id in cfg.vaults or vaults.path_of(vault_id).exists() or github.exists(vault_id):
        raise VlError(f"{vault_id} already exists.")
    folder = (args.folder or "").strip("/")
    if folder and _bad_path(folder):
        raise VlError(f"--folder: {args.folder!r} should be a folder in the drive, like Finance/2024.")
    if "workflow" not in github.scopes():
        raise VlError("Your GitHub sign-in can't add workflow files, and the vault needs one. "
                      "Run `gh auth refresh -h github.com -s workflow`, then try again.")
    say("Sign in as the bot account: the Google account that only reads the shared drive.")
    refresh = google.login(args.client_id, args.client_secret)
    access = google.access_token(args.client_id, args.client_secret, refresh)
    drive_id, drive_name = google.find_shared_drive(google.shared_drives(access), args.shared_drive)
    source = {"kind": SOURCE, "shared_drive": drive_id, "shared_drive_name": drive_name, "folder": folder,
              "max_size": MAX_SIZE, "google_client_id": args.client_id, "google_client_secret": args.client_secret}
    about = args.about or f"The text of every file in the {drive_name} shared drive. Claude only reads it."
    path = vaults.path_of(vault_id)
    path.mkdir(parents=True)
    try:
        (path / vaults.VAULT_FILE).write_text(vaults.render_vault_toml(about, source=source, source_comments={
            "folder": "the whole drive" if not folder else None,
            "google_client_secret": "a desktop app's secret; Google doesn't treat it as secret"}))
        (path / ".github" / "workflows").mkdir(parents=True)
        (path / ".github" / "workflows" / WORKFLOW_FILE).write_text(workflow())
        gitsync.init_repo(path)
        say(f"Creating the private GitHub repo {vault_id}")
        github.create_private_repo(vault_id, path)
    except VlError:
        import shutil

        shutil.rmtree(path, ignore_errors=True)
        raise
    github.set_secret(vault_id, "VL_GDRIVE_TOKEN", refresh)
    github.run_workflow(vault_id, WORKFLOW_FILE)
    _record(owner, [*_discovery().get(owner, {}).get("vaults", []), vault_id])
    _apply(config.load())
    say(f"\nMade {vault_id}, filled from {drive_name} every hour by a GitHub Action.")
    say(f"The first run is starting. Watch it with `gh run watch --repo {vault_id}`.")
    say(f"Give people read access to the repo on GitHub; `vl sync` finds it for them. Files: {contract(path)}")


# ---------------------------------------------------------------- vl gdrive fetch / login / rebuild

def _vault(ref: str):
    from .. import config

    v = config.load().vault(ref)
    _source(v)
    return v


def _signed_in(v, source: dict) -> str:
    """An access token for your own read-only sign-in for this vault's Google app. Signs in if needed."""
    from .. import google
    from ..util import VlError

    cid, secret = source["google_client_id"], source["google_client_secret"]
    refresh = google.load_token(cid)
    if not refresh:
        refresh = google.login(cid, secret)
        google.save_token(cid, refresh)
    try:
        access = google.access_token(cid, secret, refresh)
    except VlError as e:
        raise VlError(f"{e} Run `vl gdrive login {v.id}`.") from None
    google.check_read_only(access)
    return access


def check_fetch_path(path) -> None:
    from ..util import VlError

    if not isinstance(path, str) or not path.strip():
        raise VlError("Give the file's path in Drive, from the note's frontmatter.")
    if _bad_path(path):
        raise VlError(f"{path!r} isn't a path inside the drive. Use the `path` from the note's frontmatter.")


def cmd_fetch(args) -> None:
    from .. import config, google
    from ..util import VlError, fetch_dir, say

    v = _vault(args.vault)
    source = _source(v)
    check_fetch_path(args.path)
    note = next((n for n in read_notes(v.path) if n.meta.get("path") == args.path), None)
    if note is None:
        raise VlError(f"No note in {v.id} has the path {args.path!r}. Use the `path` from the note's frontmatter.")
    if not note.meta.get("id"):
        raise VlError(f"The note for {args.path!r} has no Drive ID, so it can't be fetched.")
    access = _signed_in(v, source)
    dest_root = fetch_dir(v.id)
    dest = (dest_root / args.path).resolve()
    if not str(dest).startswith(str(dest_root.resolve()) + os.sep):
        raise VlError(f"{args.path!r} isn't a path inside the drive.")
    short = config.load().shorts[v.id]
    try:
        meta = google.file_meta(access, note.meta["id"])
        out = google.download(access, note.meta["id"], meta.get("mimeType") or "", dest)
    except google.NoAccess:
        raise VlError(f"You can read {short}, but your Google account can't open this file in Drive. "
                      f"Ask for access to {source.get('shared_drive_name') or 'the shared drive'}.") from None
    os.utime(out)  # `vl sync` cleans by the time it was fetched
    out.chmod(0o444)
    say(str(out))


def cmd_login(args) -> None:
    from .. import google
    from ..util import say

    v = _vault(args.vault)
    source = _source(v)
    refresh = google.login(source["google_client_id"], source["google_client_secret"])
    google.save_token(source["google_client_id"], refresh)
    say(f"Signed in to Google for {v.id}, read-only. `vl gdrive fetch` can get originals now.")


def cmd_rebuild(args) -> None:
    from .. import github
    from ..util import say

    v = _vault(args.vault)
    github.run_workflow(v.id, WORKFLOW_FILE, {"rebuild": "true"})
    say(f"Started a full rebuild of {v.id} on GitHub. Watch it with `gh run watch --repo {v.id}`.")


def commands(subparsers) -> None:
    import argparse

    g = subparsers.add_parser("gdrive", help="vaults filled from Google Drive")
    gsub = g.add_subparsers(dest="gdrive_command", required=True, metavar="ACTION")
    s = gsub.add_parser("add", help="(admins) a vault filled from a shared drive by a GitHub Action")
    s.add_argument("vault", metavar="OWNER/vault-NAME")
    s.add_argument("--shared-drive", required=True, metavar="NAME_OR_ID")
    s.add_argument("--folder", metavar="PATH", help="only this folder of the drive")
    s.add_argument("--client-id", required=True, help="the Google OAuth app's client ID (desktop type)")
    s.add_argument("--client-secret", required=True, help="its secret")
    s.add_argument("--about", help="one line about the vault, for Claude")
    s.set_defaults(func=cmd_add)
    s = gsub.add_parser("fetch", help="download one original into the fetch folder and print where it is")
    s.add_argument("vault", metavar="OWNER/vault-NAME")
    s.add_argument("path", help="the `path` from the note's frontmatter")
    s.set_defaults(func=cmd_fetch)
    s = gsub.add_parser("login", help="sign in to Google again, for fetching")
    s.add_argument("vault", metavar="OWNER/vault-NAME")
    s.set_defaults(func=cmd_login)
    s = gsub.add_parser("rebuild", help="start the vault's workflow, writing every note again")
    s.add_argument("vault", metavar="OWNER/vault-NAME")
    s.set_defaults(func=cmd_rebuild)
    s = gsub.add_parser("run", help="only for the GitHub Action: convert, commit and push")
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--path", help=argparse.SUPPRESS)
    s.add_argument("--vault", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_run)


# ---------------------------------------------------------------- vl's other commands

def sync(v, stamp) -> str:
    """A Drive vault only takes what the Action pushed."""
    from .. import gitsync

    return gitsync.pull_keeping_changes(v.path)


def status(v) -> str:
    from .. import gitsync

    when = gitsync.git(v.path, "log", "-1", "--format=%cr", f"--grep={COMMIT}", check=False).stdout.strip()
    return f"filled from Google Drive, updated {when or 'never'}"


def joined(cfg, owner: str) -> None:
    """After `vl org join`: offer the Google sign-in for fetching originals."""
    import sys

    from .. import google
    from ..util import VlError, say

    for vid, v in sorted(cfg.vaults.items()):
        if v.owner != owner or (v.source or {}).get("kind") != SOURCE or validate_source(v.source):
            continue
        if google.load_token(v.source["google_client_id"]):
            continue
        if sys.stdin.isatty():
            answer = input(f"{vid} holds notes from Google Drive. Sign in to Google now, to fetch originals? [y/N] ")
            if answer.strip().lower() in ("y", "yes"):
                try:
                    google.save_token(v.source["google_client_id"],
                                      google.login(v.source["google_client_id"], v.source["google_client_secret"]))
                    continue
                except VlError as e:
                    say(f"Google sign-in failed: {e}")
        say(f"{vid} holds notes from Google Drive. To fetch originals, run `vl gdrive login {vid}`.")


def doctor_sources(cfg, check) -> None:
    from .. import google
    from ..util import say

    drives = [(vid, v) for vid, v in sorted(cfg.vaults.items()) if (v.source or {}).get("kind") == SOURCE]
    if not drives:
        return
    say("Google Drive vaults")
    for vid, v in drives:
        problems = validate_source(v.source)
        check(not problems, f"{vid}: [source] in vault.toml is complete", "; ".join(problems))
        if not problems:
            signed = google.load_token(v.source["google_client_id"]) is not None
            say(f"  {'ok  ' if signed else 'note'}  {vid}: " +
                ("signed in to Google for fetching" if signed else f"not signed in for fetching (`vl gdrive login {vid}`)"))
