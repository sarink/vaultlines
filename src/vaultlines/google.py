"""Google: read-only sign-in, the token check, and the few Drive API calls vl makes.

Standard library only. vl only ever asks for the `drive.readonly` scope, and checks with
Google what each token can do before using it: a token that could change Drive is
refused. Tokens never appear in messages.

Sign-ins for fetching originals are saved in ~/.vaultlines/google/, one per Google app
(client ID). The hook keeps Claude sessions away from that folder.

Tests set VAULTLINES_FAKE_GOOGLE to a fake Google's address; only one on this computer
(127.0.0.1 or localhost) is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .util import VlError, google_dir

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
READ_ONLY_SCOPES = {SCOPE, "https://www.googleapis.com/auth/drive.metadata.readonly"}
# Google's own files are exported: Docs, Sheets and Slides to Office files, Drawings to PDF.
EXPORTS = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}
LOGIN_TIMEOUT = 300


class NoAccess(VlError):
    """Drive said 403 or 404: this Google account can't open the file."""


def _fake() -> str | None:
    url = os.environ.get("VAULTLINES_FAKE_GOOGLE") or ""
    m = re.fullmatch(r"http://(127\.0\.0\.1|localhost):(\d+)/?", url)
    return url.rstrip("/") if m else None


def urls() -> dict[str, str]:
    fake = _fake()
    if fake:
        return {"auth": f"{fake}/auth", "token": f"{fake}/token", "tokeninfo": f"{fake}/tokeninfo",
                "drive": f"{fake}/drive/v3"}
    return {"auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
            "tokeninfo": "https://oauth2.googleapis.com/tokeninfo", "drive": "https://www.googleapis.com/drive/v3"}


class _HTTPError(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


def _request(url: str, data: dict | None = None, access: str | None = None, timeout: int = 60):
    """GET (or POST a form). Returns the open response. Errors never include the URL, which may hold a token."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    if access:
        req.add_header("Authorization", f"Bearer {access}")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise _HTTPError(e.code) from None
    except (urllib.error.URLError, OSError):
        raise VlError("Couldn't reach Google, so nothing was done. Try again when you're online.") from None


def _json(url: str, data: dict | None = None, access: str | None = None) -> dict:
    with _request(url, data, access) as response:
        return json.loads(response.read() or b"{}")


# ---------------------------------------------------------------- sign-in

def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _open_browser(url: str) -> None:
    if _fake():  # the fake Google approves at once
        threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=30).read(), daemon=True).start()
        return
    import webbrowser

    webbrowser.open(url)


def login(client_id: str, client_secret: str, timeout: int = LOGIN_TIMEOUT) -> str:
    """Sign in to Google in the browser with read-only access to Drive (loopback, PKCE).
    Returns the refresh token, after checking with Google that it can only read."""
    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if "code" in query or "error" in query:
                got.update(query)
            body = b"vl: you can close this tab and go back to the terminal.\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    url = urls()["auth"] + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state})
    print(f"Sign in to Google in your browser (read-only access to Drive). If it didn't open:\n  {url}",
          file=sys.stderr)
    _open_browser(url)
    deadline = time.time() + timeout
    try:
        while not got and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not got:
        raise VlError("No Google sign-in arrived in time. Try again.")
    if got.get("error") or got.get("state") != state:
        raise VlError(f"Google sign-in didn't finish ({got.get('error') or 'the reply did not match'}).")
    try:
        tokens = _json(urls()["token"], {"code": got["code"], "client_id": client_id, "client_secret": client_secret,
                                          "redirect_uri": redirect, "grant_type": "authorization_code",
                                          "code_verifier": verifier})
    except _HTTPError as e:
        raise VlError(f"Google didn't accept the sign-in (HTTP {e.status}). Check the client ID and secret.") from None
    if not tokens.get("refresh_token") or not tokens.get("access_token"):
        raise VlError("Google didn't send a lasting sign-in. Try again.")
    check_read_only(tokens["access_token"])
    return tokens["refresh_token"]


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """A fresh access token for a saved sign-in."""
    try:
        data = _json(urls()["token"], {"client_id": client_id, "client_secret": client_secret,
                                       "refresh_token": refresh_token, "grant_type": "refresh_token"})
    except _HTTPError as e:
        raise VlError(f"Google didn't accept the saved sign-in (HTTP {e.status}). Sign in again.") from None
    if not data.get("access_token"):
        raise VlError("Google didn't send an access token. Sign in again.")
    return data["access_token"]


def check_read_only(access: str) -> None:
    """Refuse unless Google says the token can only read Drive. Fails closed."""
    try:
        info = _json(urls()["tokeninfo"] + "?" + urllib.parse.urlencode({"access_token": access}))
    except _HTTPError:
        raise VlError("Google didn't accept the token, so vl can't check that it's read-only.") from None
    scopes = str(info.get("scope") or "").split()
    writes = [s for s in scopes if s not in READ_ONLY_SCOPES]
    if writes or not scopes:
        raise VlError(f"This Google sign-in can change Google Drive (scope: {' '.join(writes) or 'none'}). "
                      "vl only uses read-only sign-ins.")


# ---------------------------------------------------------------- saved sign-ins

def token_path(client_id: str) -> Path:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", client_id.removesuffix(".apps.googleusercontent.com"))
    return google_dir() / f"{name}.json"


def save_token(client_id: str, refresh_token: str) -> None:
    folder = google_dir()
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = token_path(client_id)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"client_id": client_id, "refresh_token": refresh_token, "scope": SCOPE}, f)
    tmp.replace(path)


def load_token(client_id: str) -> str | None:
    try:
        data = json.loads(token_path(client_id).read_text())
    except (OSError, ValueError):
        return None
    return data.get("refresh_token") if data.get("client_id") == client_id else None


# ---------------------------------------------------------------- Drive

def _drive(path: str, access: str, **params) -> dict:
    url = f"{urls()['drive']}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        return _json(url, access=access)
    except _HTTPError as e:
        if e.status in (403, 404):
            raise NoAccess(f"Drive says HTTP {e.status}") from None
        raise VlError(f"Google Drive said HTTP {e.status}.") from None


def shared_drives(access: str) -> list[dict]:
    out, page = [], None
    while True:
        params = {"pageSize": "100", **({"pageToken": page} if page else {})}
        data = _drive("drives", access, **params)
        out += data.get("drives", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def find_shared_drive(drives: list[dict], wanted: str) -> tuple[str, str]:
    """(ID, name) of the shared drive named or with the ID `wanted`."""
    for d in drives:
        if wanted in (d.get("id"), d.get("name")):
            return d["id"], d.get("name") or d["id"]
    names = ", ".join(repr(d.get("name")) for d in drives) or "none"
    raise VlError(f"No shared drive named {wanted!r}. This account has: {names}")


def file_meta(access: str, file_id: str) -> dict:
    return _drive(f"files/{urllib.parse.quote(file_id)}", access, fields="id,name,mimeType,size",
                  supportsAllDrives="true")


def download(access: str, file_id: str, mime: str, dest: Path) -> Path:
    """Download one file to `dest`. Google's own files are exported, and get the export's
    extension in place of dest's. Returns where it went."""
    quoted = urllib.parse.quote(file_id)
    if mime in EXPORTS:
        export, ext = EXPORTS[mime]
        dest = dest.with_suffix(ext) if dest.suffix.lower() in (".md", ".xlsx", ".pdf", ".docx", ".pptx") \
            else dest.with_name(dest.name + ext)
        url = f"{urls()['drive']}/files/{quoted}/export?" + urllib.parse.urlencode(
            {"mimeType": export, "supportsAllDrives": "true"})
    elif mime.startswith("application/vnd.google-apps."):
        raise VlError(f"This kind of Google file ({mime.rsplit('.', 1)[-1]}) can't be downloaded. Open it in Drive.")
    else:
        url = f"{urls()['drive']}/files/{quoted}?" + urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.vl-tmp")
    try:
        with _request(url, access=access, timeout=600) as response, open(tmp, "wb") as f:
            while chunk := response.read(1 << 20):
                f.write(chunk)
    except _HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.status in (403, 404):
            raise NoAccess(f"Drive says HTTP {e.status}") from None
        raise VlError(f"Google Drive said HTTP {e.status}.") from None
    if dest.exists():
        dest.chmod(0o644)
    tmp.replace(dest)
    return dest
