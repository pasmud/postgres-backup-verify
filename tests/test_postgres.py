"""Integration coverage with a real local cluster and a synthetic dump."""

import json
import os
import shutil

import pytest

import verify_backup as backup


def test_restore_and_check_synthetic_archive(tmp_path, capsys):
    missing = [tool for tool in (*backup.TOOLS, "pg_dump") if not shutil.which(tool)]
    if missing or os.geteuid() == 0:
        message = f"requires PostgreSQL tools and an unprivileged user (missing: {missing})"
        if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail(message)
        pytest.skip(message)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    tools = backup.resolve_tools()
    dump = tmp_path / "sample.dump"
    with backup.local_cluster(scratch, tools) as (connection, env):
        backup.run_command(
            [
                tools["psql"], *connection, "-d", backup.DATABASE, "-X", "-v", "ON_ERROR_STOP=1",
                "-c", "CREATE TABLE public.labels (id integer PRIMARY KEY, label text NOT NULL)",
                "-c", "INSERT INTO public.labels VALUES (1, 'synthetic-a'), (2, 'synthetic-b')",
            ],
            env,
        )
        backup.run_command(
            ["pg_dump", *connection, "-d", backup.DATABASE, "-Fc", "-f", str(dump)],
            env,
        )
    assert list(scratch.iterdir()) == []

    args = [str(dump), "--scratch-dir", str(scratch), "--json"]
    assert backup.main([*args, "--expect-rows", "labels=2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    assert report["checks"][0]["observed_rows"] == 2
    assert report["checks"][0]["observed_rows_is_lower_bound"] is False

    assert backup.main([*args, "--expect-rows", "labels=1", "--expect-table", "missing"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["checks"][0]["observed_rows_is_lower_bound"] is True
    assert report["checks"][1]["table_exists"] is False
    assert list(scratch.iterdir()) == []
