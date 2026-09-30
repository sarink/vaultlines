
from vaultlines.audience import Audience
from vaultlines.cli import preview
from vaultlines.config import Folder
from vaultlines.gitsync import repo_key

ME = "sam"


def people(*logins):
    return Audience("people", tuple(logins))


AUDS = {
    "founders": people("sam", "lee"),
    "everyone": people("sam", "lee", "ana", "raj"),
    "side": Audience("me"),
    "handbook": Audience("unknown", reason="you have read-only access"),
    "public": Audience("everyone"),
}


def test_preview_names_who_would_newly_see_notes():
    assert preview(Folder("/f", "founders", ["everyone"]), AUDS, ME) == ["writes to everyone always ask (it's in reads)"]
    lines = preview(Folder("/f", "everyone", ["founders", "public"]), AUDS, ME)
    assert lines[0] == "writes to everyone ask after reading founders (ana and raj can't see founders)"
    assert "writes to public always ask (it's in reads)" in lines


def test_preview_only_you_and_unknown():
    assert preview(Folder("/f", "side", ["everyone"]), AUDS, ME)[:-1] == []
    lines = preview(Folder("/f", "founders", ["handbook"]), AUDS, ME)
    assert lines[0].startswith("writes to founders ask after reading handbook (lee can't see handbook)")


def test_repo_key_matches_https_and_ssh():
    assert repo_key("https://github.com/Acme/Notes.git") == repo_key("git@github.com:acme/notes.git") == "acme/notes"
    assert repo_key("ssh://git@github.com/acme/notes") == "acme/notes"


def test_audience_json_round_trip():
    a = Audience("people", ("a", "b"), "", 5.0)
    assert Audience.from_json(a.to_json()) == a
