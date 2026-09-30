from vaultlines import label as lbl

ME = "sam"
FOUNDERS = {"kind": "people", "logins": ["sam", "lee"]}
TEAM = {"kind": "people", "logins": ["sam", "lee", "Ana"]}
MINE = {"kind": "me"}
PUBLIC = {"kind": "everyone"}
UNKNOWN = {"kind": "unknown", "reason": "read-only"}


def test_narrow():
    assert lbl.narrow(lbl.EVERYONE, PUBLIC) is lbl.EVERYONE
    assert lbl.narrow(lbl.EVERYONE, TEAM) == {"sam", "lee", "ana"}
    assert lbl.narrow(lbl.narrow(lbl.EVERYONE, TEAM), FOUNDERS) == {"sam", "lee"}
    assert lbl.narrow(lbl.EVERYONE, MINE) == lbl.ONLY_YOU
    assert lbl.narrow(lbl.EVERYONE, UNKNOWN) == lbl.ONLY_YOU
    assert lbl.narrow(frozenset({"lee"}), PUBLIC) == {"lee"}


def test_new_people():
    assert lbl.new_people(lbl.EVERYONE, TEAM, ME) == []
    assert lbl.new_people(frozenset({"sam", "lee"}), TEAM, ME) == ["Ana"]
    assert lbl.new_people(frozenset({"sam", "lee", "ana"}), FOUNDERS, ME) == []
    assert lbl.new_people(lbl.ONLY_YOU, MINE, ME) == []
    assert lbl.new_people(lbl.ONLY_YOU, FOUNDERS, ME) == ["lee"]
    assert "public" in lbl.new_people(lbl.ONLY_YOU, PUBLIC, ME)[0]
    assert "read-only" in lbl.new_people(frozenset({"lee"}), UNKNOWN, ME)[0]


def test_unknown_login_is_stricter():
    assert lbl.new_people(lbl.ONLY_YOU, {"kind": "people", "logins": ["sam"]}, "") == ["sam"]
    assert lbl.new_people(lbl.ONLY_YOU, {"kind": "people", "logins": ["sam"]}, ME) == []


def test_describe_and_json():
    assert lbl.describe(lbl.EVERYONE) == "everyone"
    assert lbl.describe(lbl.ONLY_YOU) == "only you"
    assert lbl.describe(frozenset({"lee", "ana"})) == "ana, lee and you"
    assert lbl.describe(frozenset({"sam", "lee"}), "Sam") == "lee and you"
    assert lbl.describe(frozenset({"sam"}), "sam") == "only you"
    for label in (lbl.EVERYONE, lbl.ONLY_YOU, frozenset({"lee"})):
        assert lbl.from_json(lbl.to_json(label)) == label


def test_session_state_round_trip_and_cleanup(tmp_path):
    with lbl.session(tmp_path, "abc-123") as box:
        assert box[0] is None
        box[0] = lbl.new_session("/f", lbl.ONLY_YOU, "fork")
    with lbl.session(tmp_path, "abc-123") as box:
        assert box[0]["source"] == "fork"
    with lbl.session(tmp_path, "../../etc/passwd") as box:  # odd ids become a hash
        box[0] = lbl.new_session(None, lbl.EVERYONE, "startup")
    assert all(p.parent == tmp_path / "sessions" for p in (tmp_path / "sessions").iterdir())
    assert [s["source"] for _, s in lbl.all_sessions(tmp_path)] == ["startup", "fork"]
    assert lbl.clean_sessions(tmp_path, max_age=-1) == 2
    assert lbl.all_sessions(tmp_path) == []
