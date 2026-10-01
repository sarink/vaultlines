"""The Google Drive source: one markdown note per Drive file, originals fetched on demand.

Each run lists Drive, downloads only new and changed files to a temporary folder, turns
them into text with markitdown, writes one note per file and deletes the downloads. The
vault holds only notes. Each note's frontmatter points to its original, which
`vl fetch VAULT PATH` downloads when Claude needs it.

vl only uses read-only rclone remotes. Before every run and every fetch it asks Google
what the remote's token can do, and refuses if it can change Drive. Sessions never run
rclone on the remote themselves: `guard()` blocks that (best effort; the token check is
the guarantee).

`vl hook` imports this module, so it only imports the standard library at the top.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .api import mentions

if TYPE_CHECKING:
    from ..config import Config, Vault

KEYS = {"remote", "max_size"}
EVERY = 3600
# rclone exports each Google type to the first of these it supports: Docs to .md,
# Sheets to .xlsx, Slides and Drawings to .pdf. Forms and others are left out.
EXPORT_FLAGS = ["--drive-export-formats", "md,xlsx,pdf", "--drive-skip-shortcuts"]
MAX_SIZE = "50M"  # bigger files get a note without text
MARKITDOWN = "0.1.8"
CONVERTER = f"markitdown {MARKITDOWN}"
MAX_TEXT = 200_000  # bytes of text in a note
BATCH = 100  # files downloaded and converted at once
OVERRIDES = ("team_drive", "root_folder_id")  # the only rclone settings a remote string may change
READ_ONLY_SCOPES = {"https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/drive.metadata.readonly"}
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
CLIENT_ID_HELP = "https://rclone.org/drive/#making-your-own-client-id"

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


# ---------------------------------------------------------------- remotes

@dataclass
class Remote:
    name: str | None  # None for a local folder, in tests
    overrides: dict[str, str] = field(default_factory=dict)
    path: str = ""


REMOTE_RE = re.compile(r"^([\w.+@][\w.+@ -]*?)((?:,[^:]*)?):(.*)$", re.DOTALL)


def _test_remotes() -> bool:
    return os.environ.get("VAULTLINES_TEST_REMOTES") == "1"


def parse_remote(text) -> Remote:
    """`NAME:path` or `NAME,team_drive=ID:path`. Other rclone settings are refused,
    because they could change which account or credentials rclone uses."""
    from ..util import VlError

    if isinstance(text, str) and _test_remotes() and text.startswith("/"):
        return Remote(None, {}, text)
    m = REMOTE_RE.match(text) if isinstance(text, str) else None
    if not m:
        raise VlError(f'{text!r} should be an rclone remote name and path, like "vl-acme:" or "vl-acme:Folder"')
    overrides = {}
    for item in filter(None, m.group(2).split(",")):
        key, _, value = item.partition("=")
        key = key.strip()
        if key not in OVERRIDES:
            raise VlError(f"rclone settings in a remote can't include {key}: it could change the account rclone "
                          f"uses. Only {' and '.join(OVERRIDES)} are allowed.")
        if not re.fullmatch(r"[A-Za-z0-9_-]*", value):
            raise VlError(f"{key} should be an ID: letters, digits, - and _")
        overrides[key] = value
    return Remote(m.group(1), overrides, m.group(3))


def remote_string(r: Remote) -> str:
    return r.name + "".join(f",{k}={v}" for k, v in r.overrides.items()) + ":" + r.path


def source_path(remote: str, path: str) -> str:
    """One file in a remote. It starts with the remote, so rclone never reads it as an option."""
    return remote + path if remote.endswith((":", "/")) else f"{remote}/{path}"


def parse_size(text: str) -> int:
    """rclone's sizes: 100, 1K, 50M, 2G (powers of 1024)."""
    m = re.fullmatch(r"(\d+)([KMGT]?)", text.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(text)
    return int(m.group(1)) * 1024 ** " KMGT".index(m.group(2).upper() or " ")


def validate(settings: dict) -> tuple[str, str] | None:
    from ..util import VlError

    remote = settings.get("remote")
    if not isinstance(remote, str) or not remote.strip():
        return "remote", 'missing. Give a read-only rclone remote, like remote = "vl-acme:" (`vl source add` makes one)'
    try:
        parse_remote(remote)
    except VlError as e:
        return "remote", str(e)
    max_size = settings.get("max_size")
    if max_size is not None:
        try:
            parse_size(max_size if isinstance(max_size, str) else "")
        except ValueError:
            return "max_size", 'should be a size, like "50M"'
    return None


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
        raise VlError("rclone isn't installed. Install it with `brew install rclone` (see https://rclone.org/install/).")
    return found


def config_file() -> str:
    """rclone's config file, as rclone finds it."""
    import subprocess

    from ..util import VlError

    result = subprocess.run([_binary(), "config", "file"], env=_env(), text=True, capture_output=True,
                            stdin=subprocess.DEVNULL)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        raise VlError(f"rclone couldn't say where its config is:\n{result.stderr.strip()}")
    return lines[-1]


def _errors(stderr: str) -> str:
    # rclone ends with a summary, like "NOTICE: Failed to copy: directory not found"
    lines = dict.fromkeys(re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ", "", line)
                          for line in stderr.strip().splitlines()
                          if "Attempt " not in line)  # rclone retries, repeating the same errors
    return "\n".join(list(lines)[-8:])


def _rclone(*args: str, check: bool = True, interactive: bool = False):
    """Run rclone with an explicit config file and no RCLONE_* variables."""
    import subprocess

    from ..util import VlError

    cmd = [_binary(), "--config", config_file(), *args]
    if interactive:
        # stderr has the sign-in link. stdout is dropped: `config create` prints the token there.
        result = subprocess.run(cmd, env=_env(), text=True, stdout=subprocess.DEVNULL)
    else:
        result = subprocess.run(cmd, env=_env(), text=True, capture_output=True, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise VlError(f"rclone {args[0]} failed" + (f":\n{_errors(result.stderr)}" if not interactive else "."))
    return result


# ---------------------------------------------------------------- the token check

def _config_dump() -> dict:
    return json.loads(_rclone("config", "dump").stdout or "{}")


def _refresh(name: str) -> None:
    """A read call, so rclone refreshes the remote's token and saves it."""
    _rclone("lsf", f"{name}:", "--max-depth", "1")


def _remote_config(name: str) -> dict:
    from ..util import VlError

    conf = _config_dump().get(name)
    fix = "Run `vl source add` to make a read-only one."
    if not isinstance(conf, dict):
        raise VlError(f"rclone has no remote named '{name}'. {fix}")
    if conf.get("type") != "drive":
        raise VlError(f"The rclone remote '{name}' isn't a Google Drive remote (it's {conf.get('type') or 'unknown'}), "
                      f"so vl can't check that it's read-only. {fix}")
    if conf.get("service_account_file") or conf.get("service_account_credentials") \
            or str(conf.get("client_credentials", "")).lower() == "true":
        raise VlError(f"The rclone remote '{name}' uses a service account, so vl can't check that it's read-only. {fix}")
    return conf


def _token(name: str, conf: dict) -> dict:
    from ..util import VlError

    try:
        token = json.loads(conf.get("token") or "")
        token["access_token"]
    except (ValueError, TypeError, KeyError):
        raise VlError(f"The rclone remote '{name}' isn't signed in to Google. Run `vl source add` "
                      "to make a read-only remote.") from None
    return token


def _expired(token: dict) -> bool:
    import datetime as dt

    text = str(token.get("expiry") or "")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    try:
        expiry = dt.datetime.fromisoformat(text)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        return True
    return expiry < dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)


def _scopes(name: str, access_token: str) -> list[str]:
    """Ask Google what a token can do. Messages never include the token."""
    import urllib.error
    import urllib.parse
    import urllib.request

    from ..util import VlError

    url = TOKENINFO + "?" + urllib.parse.urlencode({"access_token": access_token})
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            info = json.loads(response.read())
    except urllib.error.HTTPError:
        raise VlError(f"Google didn't accept the token of the rclone remote '{name}', so vl can't check that it's "
                      f"read-only. Sign in again with `rclone config reconnect {name}:`.") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise VlError(f"Couldn't reach Google to check that the rclone remote '{name}' is read-only, "
                      "so nothing ran. Try again when you're online.") from None
    return str(info.get("scope") or "").split()


def check_read_only(settings: dict) -> None:
    """Refuse unless Google says the remote's token can only read Drive. Fails closed."""
    from ..util import VlError

    remote = parse_remote(settings["remote"])
    if remote.name is None:
        return  # a local folder, in tests
    token = _token(remote.name, _remote_config(remote.name))
    if _expired(token):
        _refresh(remote.name)
        token = _token(remote.name, _remote_config(remote.name))
    scopes = _scopes(remote.name, token["access_token"])
    writes = [s for s in scopes if s not in READ_ONLY_SCOPES]
    if writes or not scopes:
        raise VlError(f"The rclone remote '{remote.name}' can change your Drive (scope: {' '.join(writes) or 'none'}). "
                      "vl only uses read-only remotes. Run `vl source add` to make one.")


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


def read_notes(root: Path, source: str) -> list[Note]:
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


def fetch_command(vault: str, path: str) -> str:
    """The command that fetches a file, quoted for the shell."""
    quoted = re.sub(r'([\\"$`])', r"\\\1", path)
    return f'vl fetch {vault} "{quoted}"'


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

def _list(settings: dict) -> list[File]:
    out = _rclone("lsjson", "-R", "--files-only", "--hash-type", "md5", "--fast-list", *EXPORT_FLAGS,
                  settings["remote"]).stdout
    return [File.from_json(item) for item in json.loads(out or "[]")]


def _download(settings: dict, paths: list[str], dest: str) -> str:
    """Copy these files into `dest`, in one rclone call. Returns rclone's errors, if any."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8") as listing:
        listing.write("".join(p + "\n" for p in paths))
        listing.flush()
        result = _rclone("copy", settings["remote"], dest, "--files-from-raw", listing.name, *EXPORT_FLAGS,
                         check=False)
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


def _source_name(cfg: Config, vault: Vault) -> str:
    return next(name for name, s in cfg.plugins.items() if s.get("kind") == "drive" and s.get("vault") == vault.name)


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


def run(cfg: Config, settings: dict, vault: Vault, rebuild: bool = False) -> str:
    import tempfile
    from collections import Counter

    from ..util import VlError

    source = _source_name(cfg, vault)
    max_size = settings.get("max_size") or MAX_SIZE
    check_read_only(settings)
    files = _list(settings)
    root = vault.path
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
            fresh = _meta(c.file, source, vault.name, meta.get("text", ""))
            meta.update({k: fresh[k] for k in ("title", "id", "path", "url", "fetch")})
            _write(root, c, meta, body)

    def note(c: Change, status: str = "", text: str | None = None) -> None:
        """Write a file's note: from its text, or with only a reason for having none."""
        fetch = fetch_command(vault.name, c.file.path)
        if text is not None:
            body, status = cut(text, fetch)
        if text is None or status == "no text":
            body = _reason(status, fetch, max_size, c.file)
        _write(root, c, _meta(c.file, source, vault.name, status), body)

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
            error = _download(settings, [c.file.path for c, _ in batch], got)
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


# ---------------------------------------------------------------- fetch

def check_fetch_path(path) -> None:
    from ..util import VlError

    if not isinstance(path, str) or not path.strip():
        raise VlError("Give the file's path in Drive, from the note's frontmatter.")
    if path.startswith(("/", "-", "~")) or any(c in path for c in "\n\r\0") or ".." in path.split("/"):
        raise VlError(f"{path!r} isn't a path inside the Drive folder. Use the `path` from the note's frontmatter.")


def fetch(cfg: Config, settings: dict, vault: Vault, path: str, dest: Path) -> Path:
    """Download one original into `dest` (the vault's fetch folder), read-only."""
    from ..util import VlError

    check_fetch_path(path)
    check_read_only(settings)
    src = source_path(settings["remote"], path)
    info = _rclone("lsjson", "--stat", *EXPORT_FLAGS, src, check=False)
    if info.returncode != 0:
        why = (_errors(info.stderr).splitlines() or ["not found"])[-1]
        raise VlError(f"No file at {path!r} in {settings['remote']} ({why}).")
    if json.loads(info.stdout or "{}").get("IsDir"):
        raise VlError(f"{path!r} is a folder. `vl fetch` gets one file.")
    out = (dest / path).resolve()
    if not str(out).startswith(str(dest.resolve()) + os.sep):
        raise VlError(f"{path!r} isn't a path inside the Drive folder.")
    if out.exists():
        out.chmod(0o644)
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    _rclone("copyto", src, str(out), *EXPORT_FLAGS)
    os.utime(out)  # rclone keeps Drive's time; `vl sync` cleans by the time it was fetched
    out.chmod(0o444)
    return out


# ---------------------------------------------------------------- the hook's side

RCLONE_RE = re.compile(r"(?:^|[\s;&|(`'\"])(?:\S*/)?rclone(?=\s|$)")


def guard(tool: str, args: dict, cwd: str, data: dict) -> str | None:
    """Keep sessions away from the remote and its token. Best effort, like every check of
    Bash; the token check is the guarantee."""
    vault, remote, conf = data.get("vault"), data.get("remote"), data.get("rclone_config")
    how = f"To get an original, run `vl fetch {vault} PATH`."
    secret = f"The rclone config holds the Google token vl fills {vault} with, so sessions can't use it. {how}"
    confs = {conf, os.path.realpath(conf)} if conf else set()
    path = args.get("file_path") or args.get("notebook_path") or (args.get("path") if tool in ("Grep", "Glob") else None)
    if isinstance(path, str) and path and confs:
        full = os.path.realpath(os.path.join(cwd or "/", os.path.expanduser(path)))
        if full in confs:
            return secret
    command = args.get("command") if tool == "Bash" else None
    if not isinstance(command, str):
        return None
    if any(mentions(command, c) for c in confs):
        return secret
    if RCLONE_RE.search(command):
        if re.search(r"\bconfig\b", command):
            return f"`rclone config` shows the Google token vl fills {vault} with, so sessions can't run it. {how}"
        if remote and re.search(r"(?<![\w.+@-])" + re.escape(remote) + r"[:,]", command):
            return f"Sessions can't run rclone on {remote}, the remote vl fills {vault} from. {how}"
    return None


def source_briefing(vault: str) -> str:
    return (f"`{vault}` holds notes converted from Google Drive by vl. For an original, "
            f"run `vl fetch {vault} \"<path from the note's frontmatter>\"`.")


# ---------------------------------------------------------------- setup

def data(cfg: Config, settings: dict) -> dict:
    from ..util import VlError

    try:
        conf = config_file()
    except VlError:
        conf = None
    return {"vault": settings["vault"], "remote": parse_remote(settings["remote"]).name, "rclone_config": conf}


TOOLS = {"rclone": "brew install rclone (see https://rclone.org/install/)",
         "uv": "brew install uv (see https://docs.astral.sh/uv/)"}


def require_tools() -> None:
    import shutil

    from ..util import VlError

    missing = [f"  {tool}: {how}" for tool, how in TOOLS.items() if not shutil.which(tool)]
    if missing:
        raise VlError("Install these first:\n" + "\n".join(missing))


def make_remote(name: str, client_id: str | None, client_secret: str | None) -> None:
    """A new read-only Drive remote. rclone opens the browser for Google sign-in."""
    from ..util import VlError

    if name in _config_dump():
        raise VlError(f"rclone already has a remote named '{name}'. Pass --remote {name}: to use it.")
    extra = [f"client_id={client_id}", f"client_secret={client_secret}"] if client_id else []
    _rclone("config", "create", name, "drive", "scope=drive.readonly", *extra, interactive=True)


def shared_drives(name: str) -> list[dict]:
    return json.loads(_rclone("backend", "drives", f"{name}:").stdout or "[]")


def find_shared_drive(drives: list[dict], wanted: str) -> str:
    from ..util import VlError

    for d in drives:
        if wanted in (d.get("id"), d.get("name")):
            return d["id"]
    names = ", ".join(repr(d.get("name")) for d in drives) or "none"
    raise VlError(f"No shared drive named {wanted!r}. This account has: {names}")


def doctor(cfg: Config, settings: dict, check) -> None:
    import shutil

    from ..util import VlError, say

    say(f"Google Drive ({settings['vault']})")
    for tool, how in TOOLS.items():
        found = shutil.which(tool)
        check(bool(found), f"{tool}: {found or 'not found'}", how)
    try:
        check_read_only(settings)
        check(True, f"rclone remote {settings['remote']} can only read Drive")
    except VlError as e:
        check(False, f"rclone remote {settings['remote']} can only read Drive", str(e))
