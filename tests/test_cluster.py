import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import verify_backup as backup


def test_plain_sql_rejected_before_resolving_tools(tmp_path, monkeypatch):
    archive = tmp_path / "fake.dump"
    archive.write_text("CREATE TABLE example (id int);")
    tools = Mock(side_effect=AssertionError("should not reach PostgreSQL"))
    monkeypatch.setattr(backup, "resolve_tools", tools)

    with pytest.raises(backup.VerificationError, match="custom-format"):
        backup.verify(archive, tmp_path)


def test_nonsticky_shared_scratch_rejected(tmp_path):
    tmp_path.chmod(0o777)
    with pytest.raises(backup.VerificationError, match="sticky"):
        backup.validate_scratch(tmp_path)


def test_cluster_forces_private_socket_and_stops_on_failure(tmp_path, monkeypatch):
    scratch = tmp_path.parent / "unit"
    scratch.mkdir()
    server = Mock()
    server.poll.return_value = None
    popen = Mock(return_value=server)
    monkeypatch.setattr(backup.subprocess, "Popen", popen)
    monkeypatch.setattr(backup, "run_command", Mock(return_value=subprocess.CompletedProcess([], 0)))
    monkeypatch.setenv("PGHOST", "example.invalid")
    monkeypatch.setenv("PGPASSWORD", "synthetic-secret")
    tools = {name: name for name in backup.TOOLS}

    with (
        pytest.raises(RuntimeError, match="simulated failure"),
        backup.local_cluster(scratch, tools) as (connection, env),
    ):
        socket = Path(connection[1])
        assert socket.stat().st_mode & 0o777 == 0o700
        assert "PGHOST" not in env and "PGPASSWORD" not in env
        assert env["HOME"] == str(socket)
        assert Path(env["PGPASSFILE"]).parent == socket
        argv = popen.call_args.args[0]
        assert "listen_addresses=" in argv
        assert f"unix_socket_directories={socket}" in argv
        raise RuntimeError("simulated failure")

    server.send_signal.assert_called_once_with(backup.signal.SIGINT)
    server.wait.assert_called_once()
    assert list(scratch.iterdir()) == []


def test_overlong_socket_path_cleans_up_without_starting_tools(tmp_path, monkeypatch):
    scratch = tmp_path / ("a" * 105)
    scratch.mkdir()
    run = Mock()
    monkeypatch.setattr(backup, "run_command", run)
    with pytest.raises(backup.VerificationError, match="too long"), backup.local_cluster(scratch, {}):
        pytest.fail("cluster should not start")

    run.assert_not_called()
    assert list(scratch.iterdir()) == []
