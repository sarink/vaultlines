"""A fake Google for tests: OAuth login (with PKCE), tokeninfo and the Drive API, on 127.0.0.1.

vl talks to it when VAULTLINES_FAKE_GOOGLE is its address. Its "browser" approves every
login at once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

READONLY = "https://www.googleapis.com/auth/drive.readonly"
ACCESS = "ya29.SECRET-ACCESS"
REFRESH = "1//SECRET-REFRESH"
GOOGLE_TYPES = {
    "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.google-apps.drawing": "application/pdf",
}


class FakeGoogle:
    def __init__(self):
        self.scope = READONLY
        self.client_id = "1234-abc.apps.googleusercontent.com"
        self.drives = [{"id": "0AHF8p0HI9kM1Uk9PVA", "name": "Mixim HQ"}, {"id": "0BOTHER", "name": "Other"}]
        self.files: dict[str, dict] = {}  # id -> {"name", "mimeType", "data", "status"}
        self.requests: list[str] = []
        self._challenges: dict[str, str] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def file(self, file_id: str, name: str, mime: str, data: bytes = b"", status: int = 200):
        self.files[file_id] = {"name": name, "mimeType": mime, "data": data, "status": status}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body=b"", kind="application/json", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, data):
                self._send(status, json.dumps(data).encode())

            def _authorized(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {ACCESS}"

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                fake.requests.append(self.path)
                if url.path == "/auth":
                    fake._challenges["CODE"] = q["code_challenge"]
                    assert q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
                    target = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "CODE", "state": q["state"],
                                                                                 "scope": q["scope"]})
                    return self._send(302, headers={"Location": target})
                if url.path == "/tokeninfo":
                    if q.get("access_token") != ACCESS:
                        return self._json(400, {"error": "invalid_token"})
                    return self._json(200, {"scope": fake.scope, "expires_in": 3000})
                if not self._authorized():
                    return self._json(401, {"error": "unauthorized"})
                if url.path == "/drive/v3/drives":
                    return self._json(200, {"drives": fake.drives})
                parts = url.path.split("/")  # /drive/v3/files/ID[/export]
                if url.path.startswith("/drive/v3/files/"):
                    f = fake.files.get(parts[4])
                    if f is None:
                        return self._json(404, {"error": {"code": 404, "message": "File not found"}})
                    if f["status"] != 200:
                        return self._json(f["status"], {"error": {"code": f["status"], "message": "no"}})
                    assert q.get("supportsAllDrives") == "true"
                    if len(parts) > 5 and parts[5] == "export":
                        assert q["mimeType"] == GOOGLE_TYPES[f["mimeType"]]
                        return self._send(200, b"EXPORTED " + q["mimeType"].encode(), q["mimeType"])
                    if q.get("alt") == "media":
                        return self._send(200, f["data"], f["mimeType"])
                    return self._json(200, {"id": parts[4], "name": f["name"], "mimeType": f["mimeType"],
                                            "size": str(len(f["data"]))})
                return self._json(404, {"error": "unknown"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                q = dict(urllib.parse.parse_qsl(body))
                fake.requests.append("POST " + self.path)
                if self.path != "/token" or q.get("client_id") != fake.client_id:
                    return self._json(400, {"error": "invalid_client"})
                if q["grant_type"] == "authorization_code":
                    digest = hashlib.sha256(q["code_verifier"].encode()).digest()
                    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
                    if q.get("code") != "CODE" or fake._challenges.get("CODE") != challenge:
                        return self._json(400, {"error": "invalid_grant"})
                    return self._json(200, {"access_token": ACCESS, "refresh_token": REFRESH, "scope": fake.scope,
                                            "expires_in": 3599, "token_type": "Bearer"})
                if q["grant_type"] == "refresh_token":
                    if q.get("refresh_token") != REFRESH:
                        return self._json(400, {"error": "invalid_grant"})
                    return self._json(200, {"access_token": ACCESS, "scope": fake.scope, "expires_in": 3599})
                return self._json(400, {"error": "unsupported_grant_type"})

        return Handler


if __name__ == "__main__":
    # For tests/e2e.sh: `python fake_google.py FILES.json` serves those files and prints its address.
    import sys
    import time

    fake = FakeGoogle()
    with open(sys.argv[1]) as fh:
        files = json.load(fh)
    for file_id, f in files.items():
        fake.file(file_id, f["name"], f["mimeType"], f.get("data", "").encode(), f.get("status", 200))
    print(fake.url, flush=True)
    while True:
        time.sleep(3600)
