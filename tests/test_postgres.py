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
                "-c", 'CREATE SCHEMA "Inventory"',
                "-c", 'CREATE TABLE "Inventory"."ArchivedLabels" () INHERITS (public.labels)',
                "-c", """INSERT INTO "Inventory"."ArchivedLabels" VALUES (3, 'synthetic-archive')""",
                "-c", 'CREATE TABLE "Inventory"."Empty" (id integer)',
                "-c", 'CREATE VIEW "Inventory"."LabelView" AS SELECT id, label FROM public.labels',
            ],
            env,
        )
        backup.run_command(
            ["pg_dump", *connection, "-d", backup.DATABASE, "-Fc", "-f", str(dump)],
            env,
        )
    assert list(scratch.iterdir()) == []

    args = [str(dump), "--scratch-dir", str(scratch), "--json"]
    assert backup.main([
        *args, "--expect-rows", "labels=2", "--expect-rows-at-least", "labels=1",
        "--expect-rows", "Inventory.ArchivedLabels=1", "--expect-rows", "Inventory.Empty=0",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    exact, minimum, archived, empty = report["checks"]
    assert exact["observed_rows"] == 2  # Excludes the inherited child row.
    assert exact["observed_rows_is_lower_bound"] is False
    assert minimum["kind"] == "min_rows"
    assert minimum["observed_rows"] == 1
    assert minimum["observed_rows_is_lower_bound"] is True
    assert archived["observed_rows"] == 1
    assert empty["observed_rows"] == 0
    assert list(scratch.iterdir()) == []

    assert backup.main([
        *args, "--expect-rows", "labels=1", "--expect-rows-at-least", "labels=3",
        "--expect-table", "missing", "--expect-table", "inventory.Empty",
        "--expect-table", "Inventory.LabelView",
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    excess, shortfall, missing, wrong_case, view = report["checks"]
    assert excess["status"] == "fail"
    assert excess["observed_rows_is_lower_bound"] is True
    assert shortfall["status"] == "fail"
    assert shortfall["observed_rows"] == 2
    assert shortfall["observed_rows_is_lower_bound"] is False
    assert [check["table_exists"] for check in (missing, wrong_case, view)] == [False, False, False]
    assert list(scratch.iterdir()) == []
