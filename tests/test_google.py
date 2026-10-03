"""google.py: read-only login, the token check and the Drive API, against a fake Google."""

from __future__ import annotations

import os
import stat

import pytest
from fake_google import ACCESS, REFRESH, FakeGoogle

from vaultlines import google
from vaultlines.util import VlError

CLIENT = "1234-abc.apps.googleusercontent.com"
SECRET = "GOCSPX-x"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    g = FakeGoogle()
    monkeypatch.setenv("VAULTLINES_FAKE_GOOGLE", g.url)
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path / "vl"))
    yield g
    g.close()


def no_secrets(text: str) -> bool:
    return "SECRET" not in text


def test_login_uses_pkce_and_returns_the_refresh_token(fake):
    assert google.login(CLIENT, SECRET) == REFRESH
    auth = next(r for r in fake.requests if r.startswith("/auth"))
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.readonly" in auth
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A" in auth


def test_login_refuses_a_token_that_can_change_drive(fake):
    fake.scope = "https://www.googleapis.com/auth/drive"
    with pytest.raises(VlError, match="can change Google Drive") as e:
        google.login(CLIENT, SECRET)
    assert no_secrets(str(e.value))


def test_access_token(fake):
    assert google.access_token(CLIENT, SECRET, REFRESH) == ACCESS
    with pytest.raises(VlError, match="Google didn't accept") as e:
        google.access_token(CLIENT, SECRET, "1//SECRET-WRONG")
    assert no_secrets(str(e.value))


def test_check_read_only(fake):
    google.check_read_only(ACCESS)
    fake.scope = f"{fake.scope} https://www.googleapis.com/auth/drive.metadata.readonly"
    google.check_read_only(ACCESS)
    fake.scope = "https://www.googleapis.com/auth/drive.file"
    with pytest.raises(VlError, match="can change Google Drive"):
        google.check_read_only(ACCESS)
    with pytest.raises(VlError) as e:
        google.check_read_only("ya29.SECRET-OTHER")
    assert no_secrets(str(e.value))


def test_offline_fails_closed(monkeypatch):
    monkeypatch.setenv("VAULTLINES_FAKE_GOOGLE", "http://127.0.0.1:9")  # nothing listens there
    with pytest.raises(VlError, match="Couldn't reach Google"):
        google.check_read_only(ACCESS)


@pytest.mark.parametrize("url", ["http://evil.example.com:80", "https://127.0.0.1.evil.com", "file:///tmp"])
def test_a_fake_google_must_be_on_this_computer(monkeypatch, url):
    monkeypatch.setenv("VAULTLINES_FAKE_GOOGLE", url)
    assert google.urls()["token"] == "https://oauth2.googleapis.com/token"


@pytest.fixture
def folders(fake):
    """Acme HQ (a shared drive) with Finance/2024, a folder shared with the account, and one
    in its My Drive."""
    fake.folder("FIN", "Finance", parent="0AACMEHQ1234567890", drive="0AACMEHQ1234567890")
    fake.folder("F24", "2024", parent="FIN", drive="0AACMEHQ1234567890")
    fake.folder("BOARD", "Board decks", shared=True)
    fake.folder("MINE", "Recipes", parent="MYDRIVE")
    fake.file("PDF1", "a.pdf", "application/pdf")
    return fake


def test_shared_drives(fake):
    assert [d["name"] for d in google.shared_drives(ACCESS)] == ["Acme HQ", "Other"]


def names(found):
    return [(f["id"], f["name"]) for f in found]


def test_folders_in_a_drive_a_folder_or_shared_with_the_account(folders):
    assert names(google.folders(ACCESS, "0AACMEHQ1234567890", drive_id="0AACMEHQ1234567890")) == [("FIN", "Finance")]
    assert names(google.folders(ACCESS, "FIN", drive_id="0AACMEHQ1234567890")) == [("F24", "2024")]
    assert names(google.folders(ACCESS, shared=True)) == [("BOARD", "Board decks")]
    assert names(google.folders(ACCESS, google.my_drive(ACCESS))) == [("MINE", "Recipes")]
    assert google.my_drive(ACCESS) == "MYDRIVE"


@pytest.mark.parametrize("wanted, place", [
    ("0AACMEHQ1234567890", {"id": "0AACMEHQ1234567890", "name": "Acme HQ", "drive_id": "0AACMEHQ1234567890",
                             "drive_name": "Acme HQ"}),
    ("F24", {"id": "F24", "name": "2024", "drive_id": "0AACMEHQ1234567890", "drive_name": "Acme HQ"}),
    ("BOARD", {"id": "BOARD", "name": "Board decks", "drive_id": "", "drive_name": ""}),
])
def test_find_a_folder_or_shared_drive_by_id(folders, wanted, place):
    assert google.find_folder(ACCESS, wanted) == place


@pytest.mark.parametrize("wanted, message", [
    ("PDF1", "PDF1 is a file, not a folder"),
    ("NOPE", "This Google account can't open the folder NOPE"),
])
def test_find_a_folder_refuses(folders, wanted, message):
    with pytest.raises(VlError, match=message):
        google.find_folder(ACCESS, wanted)


def test_download_a_file(fake, tmp_path):
    fake.file("F1", "Runway.pdf", "application/pdf", b"%PDF original")
    meta = google.file_meta(ACCESS, "F1")
    out = google.download(ACCESS, "F1", meta["mimeType"], tmp_path / "Finance" / "Runway.pdf")
    assert out == tmp_path / "Finance" / "Runway.pdf" and out.read_bytes() == b"%PDF original"


@pytest.mark.parametrize("mime, ext", [
    ("application/vnd.google-apps.document", ".docx"),
    ("application/vnd.google-apps.spreadsheet", ".xlsx"),
    ("application/vnd.google-apps.presentation", ".pptx"),
    ("application/vnd.google-apps.drawing", ".pdf"),
])
def test_google_files_are_exported(fake, tmp_path, mime, ext):
    fake.file("G1", "Plan", mime)
    out = google.download(ACCESS, "G1", mime, tmp_path / "Team" / "Plan.md")
    assert out == tmp_path / "Team" / f"Plan{ext}"
    assert out.read_bytes().startswith(b"EXPORTED ")


def test_google_types_that_cant_be_exported(fake, tmp_path):
    fake.file("X", "Form", "application/vnd.google-apps.form")
    with pytest.raises(VlError, match="can't be downloaded"):
        google.download(ACCESS, "X", "application/vnd.google-apps.form", tmp_path / "Form")


@pytest.mark.parametrize("status", [403, 404])
def test_no_access_to_a_file(fake, status):
    if status == 403:
        fake.file("F2", "Secret.pdf", "application/pdf", status=403)
    with pytest.raises(google.NoAccess) as e:
        google.file_meta(ACCESS, "F2")
    assert no_secrets(str(e.value))


def test_tokens_are_saved_for_vl_only(fake):
    google.save_token(CLIENT, REFRESH)
    path = google.token_path(CLIENT)
    assert path.name == "1234-abc.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert google.load_token(CLIENT) == REFRESH
    assert google.load_token("other.apps.googleusercontent.com") is None
