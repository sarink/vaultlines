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


def _fake_vl(folder):
    folder.mkdir(parents=True)
    (folder / "vl").write_text("#!/bin/sh\n")
    (folder / "vl").chmod(0o755)
    return folder / "vl"


def test_the_job_runs_the_installed_vl_not_a_projects_venv(monkeypatch, tmp_path):
    venv = _fake_vl(tmp_path / "proj" / ".venv" / "bin")  # first on PATH under `uv run`
    tool = _fake_vl(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{venv.parent}:{tool.parent}")
    assert launchd._vl_path() == str(tool)
    monkeypatch.setenv("PATH", str(venv.parent))
    assert launchd._vl_path() == str(venv)  # the only one there is


def test_apply_keeps_the_job_current(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VAULTLINES_HOME", str(tmp_path / "vl"))
    monkeypatch.delenv("VAULTLINES_NO_LAUNCHD", raising=False)
    monkeypatch.setattr(launchd, "supported", lambda: True)
    calls = []
    monkeypatch.setattr(launchd, "run", lambda cmd, **kw: calls.append(cmd))
    assert launchd.keep_current(600) is False and calls == []  # no job: sync was never set up, or turned off
    launchd.install(600)
    calls.clear()
    assert launchd.keep_current(600) is False and calls == []  # already current
    assert launchd.keep_current(300) is True  # a new sync_interval
    assert "<integer>300</integer>" in launchd.plist_path().read_text()
    assert ["launchctl", "bootstrap", f"gui/{__import__('os').getuid()}", str(launchd.plist_path())] in calls
