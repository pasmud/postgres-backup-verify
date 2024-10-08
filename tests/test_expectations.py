from contextlib import contextmanager
from itertools import repeat
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

import verify_backup as backup


@pytest.mark.parametrize("name", ["", "9items", "a" * 64, "a.b", 'a"b', "a'b", "a;--", "café"])
def test_invalid_identifiers_rejected_in_both_positions(name):
    for schema, table in ((name, "items"), ("public", name)):
        with pytest.raises(backup.VerificationError, match="schema and table names"):
            backup.TableExpectation(schema, table)


@pytest.mark.parametrize("rows", [-1, True, 1.5, "1", backup.MAX_EXPECTED_ROWS + 1])
def test_invalid_row_counts_rejected(rows):
    with pytest.raises(backup.VerificationError, match="expected rows"):
        backup.TableExpectation("public", "items", rows)


def test_too_many_expectations_rejected_before_accessing_files():
    expectation = backup.TableExpectation("public", "items")
    with pytest.raises(backup.VerificationError, match="at most"):
        backup.verify(None, None, repeat(expectation))


def test_results_and_bounded_local_queries(monkeypatch):
    expectations = [
        backup.TableExpectation("public", "missing", 0),
        backup.TableExpectation("Demo", "Items"),
        backup.TableExpectation("Demo", "Empty", 0),
        backup.TableExpectation("Demo", "Items", 2),
        backup.TableExpectation("Demo", "Items", 3),
        backup.TableExpectation("Demo", "Items", 1),
    ]
    replies = ("f", "t", "t", "0", "t", "2", "t", "2", "t", "2")
    run = Mock(side_effect=[CompletedProcess([], 0, value + "\n") for value in replies])
    monkeypatch.setattr(backup, "run_command", run)
    connection = ["-h", "/tmp/pgv-synthetic", "-p", "5432", "-U", backup.ROLE]
    env = {"HOME": connection[1]}

    results = backup.check_expectations(expectations, "/bin/psql", connection, env)

    assert [result.expectation for result in results] == expectations
    assert [result.passed for result in results] == [False, True, True, True, False, False]
    assert [result.table_exists for result in results] == [False, True, True, True, True, True]
    assert [result.row_count for result in results] == [None, None, 0, 2, 2, 2]
    assert [result.row_count_is_lower_bound for result in results] == [False] * 5 + [True]
    queries = []
    for call in run.call_args_list:
        argv, query_env = call.args
        assert argv[: 1 + len(connection)] == ["/bin/psql", *connection]
        assert argv[7:-2] == ["-d", backup.DATABASE, "-X", "-w", "-qAt", "-v", "ON_ERROR_STOP=1"]
        assert argv[-2] == "-c"
        assert call.kwargs["timeout"] == backup.CHECK_TIMEOUT
        assert "default_transaction_read_only=on" in query_env["PGOPTIONS"]
        assert "statement_timeout=5000" in query_env["PGOPTIONS"]
        queries.append(argv[-1])
    assert "PGOPTIONS" not in env
    assert "c.relkind = 'r'" in queries[0]
    assert 'ONLY "Demo"."Empty" LIMIT 1' in queries[3]
    assert 'ONLY "Demo"."Items" LIMIT 3' in queries[5]
    assert 'ONLY "Demo"."Items" LIMIT 2' in queries[-1]


@pytest.mark.parametrize("replies", [("?",), ("t", "3"), ("t", "error")])
def test_invalid_query_outputs_are_operation_errors(monkeypatch, replies):
    run = Mock(side_effect=[CompletedProcess([], 0, value) for value in replies])
    monkeypatch.setattr(backup, "run_command", run)
    expectation = backup.TableExpectation("public", "items", 1)
    with pytest.raises(backup.VerificationError, match="unexpected"):
        backup.check_expectations([expectation], "psql", [], {})


def test_checks_run_after_restore_and_use_the_live_cluster(tmp_path, monkeypatch):
    archive = tmp_path / "synthetic.dump"
    archive.write_bytes(b"PGDMP")
    monkeypatch.setattr(backup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(backup, "validate_scratch", lambda path: path)
    monkeypatch.setattr(backup, "resolve_tools", lambda: {name: name for name in backup.TOOLS})
    connection = ["-h", str(tmp_path)]
    env = {"HOME": str(tmp_path)}
    calls = []

    @contextmanager
    def cluster(*_args):
        yield connection, env
        calls.append("cleanup")

    def run(argv, command_env, **_kwargs):
        assert argv[1:3] == connection
        assert command_env["HOME"] == env["HOME"]
        calls.append(argv[0])
        return CompletedProcess(argv, 0, "t")

    monkeypatch.setattr(backup, "local_cluster", cluster)
    monkeypatch.setattr(backup, "run_command", run)

    results = backup.verify(archive, tmp_path, [backup.TableExpectation("public", "items")])

    assert results[0].passed
    assert calls == ["pg_restore", "psql", "cleanup"]
