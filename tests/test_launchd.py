from vaultlines import launchd


def test_uninstall_leaves_launchd_alone_when_turned_off(monkeypatch):
    calls = []
    monkeypatch.setattr(launchd, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setenv("VAULTLINES_NO_LAUNCHD", "1")
    launchd.uninstall()
    assert calls == []


def test_the_log_is_in_the_home(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path))
    assert launchd.log_path() == tmp_path / "state" / "sync.log"


def test_the_job_keeps_a_custom_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(launchd, "run", lambda cmd, **kw: None)
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path / "vl"))
    launchd.install(600)
    plist = launchd.plist_path().read_text()
    assert f"<key>VAULTLINES_HOME</key><string>{tmp_path / 'vl'}</string>" in plist
    monkeypatch.delenv("VAULTLINES_HOME")
    launchd.install(600)
    assert "VAULTLINES_HOME" not in launchd.plist_path().read_text()
