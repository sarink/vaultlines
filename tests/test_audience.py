from vaultlines.audience import Audience
from vaultlines.cli import preview

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
    assert preview("founders", ["everyone"], AUDS, ME) == []
    lines = preview("everyone", ["founders", "public"], AUDS, ME)
    assert lines == ["writes to everyone ask after reading founders (ana and raj can't see founders)"]


def test_preview_only_you_and_unknown():
    assert preview("side", ["everyone"], AUDS, ME) == []
    lines = preview("founders", ["handbook"], AUDS, ME)
    assert lines[0].startswith("writes to founders ask after reading handbook (lee can't see handbook)")


def test_audience_json_round_trip():
    a = Audience("people", ("a", "b"), "", 5.0)
    assert Audience.from_json(a.to_json()) == a
