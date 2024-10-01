"""Replay synthetic catalog and psql vectors through the installed CLI code."""

import json
import re
import subprocess
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import pytest

import verify_backup as backup

VECTORS = sorted(Path(__file__).with_name("regression_vectors").glob("*.json"))
LOOKUP = re.compile(
    r"SELECT EXISTS \(SELECT 1 FROM pg_catalog\.pg_class AS c "
    r"JOIN pg_catalog\.pg_namespace AS n ON n\.oid = c\.relnamespace "
    r"WHERE n\.nspname = '(\w+)' AND c\.relname = '(\w+)' AND c\.relkind = 'r'\)"
)
COUNT = re.compile(
    r"SELECT pg_catalog\.count\(\*\) FROM \(SELECT 1 FROM (ONLY )?"
    r'"(\w+)"\."(\w+)" LIMIT ([0-9]+)\) AS bounded_rows'
)
CONNECTION = ["-h", "/synthetic/socket", "-p", "5432", "-U", "backup_verify"]
ENV = {"HOME": "/synthetic/socket"}
PSQL_ARGS = ["-d", "postgres", "-X", "-w", "-qAt", "-v", "ON_ERROR_STOP=1", "-c"]


def forbid_postgres(*_args, **_kwargs):
    pytest.fail("regression vectors must never launch PostgreSQL")


def install_fake_psql(monkeypatch, vector):
    pending = deque(vector["queries"])
    fault = vector.get("psql_reply", {})
    catalog = vector.get("catalog", {})
    restored = False
    calls = 0

    @contextmanager
    def cluster(_scratch, _tools):
        yield CONNECTION.copy(), ENV.copy()

    def execute(argv, *, env, timeout, **kwargs):
        nonlocal restored, calls
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["capture_output"]
        if argv[0] == "pg_restore":
            assert not restored and calls == 0
            assert argv[1:7] == CONNECTION
            restored = True
            return subprocess.CompletedProcess(argv, 0, "", "")

        assert restored
        assert argv[:-1] == ["psql", *CONNECTION, *PSQL_ARGS]
        assert env == {
            **ENV,
            "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=5000 "
            "-c search_path=pg_catalog",
        }
        assert timeout == 10
        assert pending, f"unexpected extra query: {argv[-1]}"
        expected = pending.popleft()
        sql = argv[-1]
        if lookup := LOOKUP.fullmatch(sql):
            name = ".".join(lookup.groups())
            assert [name] == expected
            stdout = "t\n" if catalog.get(name, {}).get("kind") == "r" else "f\n"
        else:
            count = COUNT.fullmatch(sql)
            assert count, f"unexpected psql query: {sql}"
            only, schema, table, limit = count.groups()
            name = f"{schema}.{table}"
            assert [name, int(limit)] == expected
            relation = catalog[name]
            assert relation["kind"] == "r"
            # A count without ONLY would include the synthetic descendants.
            inherited = 0 if only else relation.get("descendant_rows", 0)
            total = relation["rows"] + inherited
            stdout = f"{min(total, int(limit))}\n"

        calls += 1
        if fault.get("call") == calls:
            if fault.get("timeout"):
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(
                argv,
                fault.get("exit", 0),
                fault.get("stdout", stdout),
                fault.get("stderr", ""),
            )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def assert_complete():
        assert restored, "the fake restore was not invoked"
        assert not pending, f"missing queries: {list(pending)}"

    tools = {"psql": "psql", "pg_restore": "pg_restore"}
    monkeypatch.setattr(backup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(backup, "resolve_tools", lambda: tools)
    monkeypatch.setattr(backup, "local_cluster", cluster)
    monkeypatch.setattr(backup.subprocess, "run", execute)
    return assert_complete


@pytest.mark.parametrize("path", VECTORS, ids=lambda path: path.stem)
def test_regression_vector(path, monkeypatch, capsys, tmp_path):
    vector = json.loads(path.read_text(encoding="utf-8"))
    monkeypatch.setattr(backup.subprocess, "Popen", forbid_postgres)
    monkeypatch.setattr(backup.subprocess, "run", forbid_postgres)

    if "api_args" in vector:
        with pytest.raises(backup.VerificationError) as error:
            backup.TableExpectation(*vector["api_args"])
        assert str(error.value) == vector["error"]
        return

    archive = tmp_path / "synthetic.dump"
    args = [str(archive), "--scratch-dir", str(tmp_path), *vector["args"]]
    if "usage_error" in vector:
        monkeypatch.setattr(backup, "verify", forbid_postgres)
        with pytest.raises(SystemExit) as error:
            backup.main([*args, "--json"])
        assert error.value.code == 2
        out, err = capsys.readouterr()
        assert not out and err.startswith("usage:")
        assert err.splitlines()[-1].partition(": error: ")[2] == vector["usage_error"]
        return

    archive.write_bytes(b"PGDMP")
    text = "\n".join(vector["text"]) + "\n"
    for as_json in (True, False):
        assert_complete = install_fake_psql(monkeypatch, vector)
        assert backup.main([*args, "--json"] if as_json else args) == vector["exit"]
        assert_complete()
        out, err = capsys.readouterr()
        if as_json:
            assert not err
            # Canonical JSON distinguishes numeric 0/1 from false/true.
            assert json.dumps(json.loads(out), sort_keys=True) == json.dumps(
                vector["json"], sort_keys=True
            )
        elif vector["exit"] == 2:
            assert (out, err) == ("", text)
        else:
            assert (out, err) == (text, "")
