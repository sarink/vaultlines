from pathlib import Path

from vaultlines.audience import Audience, check, folder_problems
from vaultlines.config import STAR, Config, Folder, Vault
from vaultlines.gitsync import repo_key

ME = "sam"


def people(*logins):
    return Audience("people", tuple(logins))


AUDS = {
    "personal": people("sam"),                      # private repo, only you
    "side": Audience("me"),                         # no remote
    "founders": people("sam", "lee"),
    "everyone": people("sam", "lee", "ana", "raj"),
    "globex": people("sam", "globex-dev1", "globex-dev2"),
    "handbook": Audience("unknown", reason="you have read-only access"),
    "public": Audience("everyone"),
}


def cfg(writes, reads):
    c = Config()
    for name in AUDS:
        c.vaults[name] = Vault(name, Path(f"/v/{name}"))
    c.folders["/f"] = Folder("/f", writes, reads)
    return c


def problems(writes, *reads):
    return folder_problems(cfg(writes, list(reads)), "/f", AUDS, ME)


def test_only_you_can_read_anything():
    assert problems("personal", "founders", "everyone", "globex", "handbook", "side") == []
    assert problems("side", "personal", "handbook") == []


def test_public_read_vault_always_passes():
    assert problems("globex", "public") == []
    assert problems("handbook", "public") == []


def test_subset_passes():
    assert problems("founders", "everyone") == []


def test_not_a_subset_is_refused_and_names_the_people():
    [p] = problems("everyone", "founders")
    assert p.why == "ana and raj can see everyone but not founders"
    [p] = problems("globex", "everyone")
    assert "globex-dev1 and globex-dev2" in p.why


def test_reading_a_local_vault_from_a_shared_one_is_refused():
    [p] = problems("founders", "side")
    assert p.why == "lee can see founders but not side"


def test_unknown_read_vault_is_refused():
    [p] = problems("founders", "handbook")
    assert "can't tell who can see handbook" in p.why
    assert "read-only" in p.why


def test_unknown_write_vault_is_refused_unless_reads_are_public():
    c = cfg("handbook", ["founders", "public"])
    [p] = folder_problems(c, "/f", AUDS, ME)
    assert p.read == "founders"
    assert folder_problems(cfg("handbook", []), "/f", AUDS, ME) == []


def test_public_write_vault_can_only_read_public():
    [p] = problems("public", "founders")
    assert p.why == "public is public and founders isn't"


def test_unknown_login_makes_the_check_stricter():
    # Without your login, a repo only you can see looks shared, so it's refused.
    assert folder_problems(cfg("personal", ["side"]), "/f", AUDS, "") != []
    assert folder_problems(cfg("personal", ["side"]), "/f", AUDS, ME) == []


def test_logins_compare_without_case():
    auds = dict(AUDS, founders=people("Sam", "LEE"))
    assert folder_problems(cfg("founders", ["everyone"]), "/f", auds, "SAM") == []


def test_check_covers_star_first():
    c = cfg("everyone", ["founders"])
    c.folders[STAR] = Folder(STAR, "globex", ["everyone"])
    assert [p.folder for p in check(c, AUDS, ME)] == [STAR, "/f"]


def test_repo_key_matches_https_and_ssh():
    assert repo_key("https://github.com/Acme/Notes.git") == repo_key("git@github.com:acme/notes.git") == "acme/notes"
    assert repo_key("ssh://git@github.com/acme/notes") == "acme/notes"
