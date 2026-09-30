from vaultlines import launchd


def test_uninstall_leaves_launchd_alone_when_turned_off(monkeypatch):
    calls = []
    monkeypatch.setattr(launchd, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setenv("VAULTLINES_NO_LAUNCHD", "1")
    launchd.uninstall()
    assert calls == []
