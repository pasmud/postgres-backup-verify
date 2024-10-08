"""Restore a trusted custom-format backup in a disposable local PostgreSQL cluster."""

import argparse
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

SOCKET_NAME = ".s.PGSQL.5432"
ROLE = "backup_verify"
DATABASE = "postgres"
TOOLS = ("initdb", "postgres", "pg_isready", "pg_restore", "psql")
COMMAND_TIMEOUT = 120
CHECK_TIMEOUT = 10
MAX_EXPECTATIONS = 32
MAX_EXPECTED_ROWS = 100_000
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


class VerificationError(Exception):
    """Invalid input or a failure to complete the verification."""


@dataclass(frozen=True)
class TableExpectation:
    schema: str
    table: str
    rows: int | None = None

    def __post_init__(self):
        for name in (self.schema, self.table):
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                raise VerificationError(
                    "schema and table names must be 1–63 ASCII letters, digits, or underscores "
                    "and start with a letter or underscore"
                )
        if self.rows is not None and (
            type(self.rows) is not int or not 0 <= self.rows <= MAX_EXPECTED_ROWS
        ):
            raise VerificationError(f"expected rows must be an integer from 0 to {MAX_EXPECTED_ROWS}")


@dataclass(frozen=True)
class CheckResult:
    expectation: TableExpectation
    table_exists: bool
    row_count: int | None = None
    row_count_is_lower_bound: bool = False

    @property
    def passed(self):
        return self.table_exists and (
            self.expectation.rows is None or self.row_count == self.expectation.rows
        )


def validate_dump(path):
    path = path.resolve(strict=True)
    if not path.is_file():
        raise VerificationError("dump must be a regular file")
    with path.open("rb") as archive:
        if archive.read(5) != b"PGDMP":
            raise VerificationError("expected a PostgreSQL custom-format dump (pg_dump -Fc)")
    return path


def validate_scratch(path):
    path = path.resolve(strict=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or path == path.parent:
        raise VerificationError("scratch must be an existing directory other than /")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", str(path)):
        raise VerificationError("scratch path may only contain ASCII letters, digits, /, _, ., and -")
    if info.st_uid not in (0, os.geteuid()) or (
        info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
    ):
        raise VerificationError("scratch must be owned by you or root and sticky if shared-writable")
    return path


def resolve_tools():
    tools = {name: shutil.which(name) for name in TOOLS}
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        raise VerificationError(f"PostgreSQL programs missing from PATH: {', '.join(missing)}")
    return tools


def clean_env(workspace):
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(workspace),
        "TMPDIR": str(workspace),
        "PGPASSFILE": str(workspace / "unused.pgpass"),
        "LC_ALL": "C",
    }


def run_command(argv, env, timeout=COMMAND_TIMEOUT, *, check=True):
    try:
        result = subprocess.run(
            argv, check=False, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(f"{Path(argv[0]).name} timed out after {timeout}s") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostics"
        raise VerificationError(f"{Path(argv[0]).name} exited {result.returncode}: {detail[-4000:]}")
    return result


def wait_until_ready(server, argv, env, logfile):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server.poll() is not None:
            break
        if run_command(argv, env, timeout=2, check=False).returncode == 0:
            return
        time.sleep(0.1)
    with logfile.open("rb") as log:
        log.seek(max(0, logfile.stat().st_size - 4000))
        detail = log.read().decode("utf-8", errors="replace").strip()
    raise VerificationError(f"disposable PostgreSQL did not start: {detail or 'no diagnostics'}")


def stop_server(server, workspace):
    if server.poll() is not None:
        return
    # Fast shutdown rolls back active connections; immediate is the fallback.
    for sig, timeout in ((signal.SIGINT, 10), (signal.SIGQUIT, 5)):
        server.send_signal(sig)
        try:
            server.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            continue
    raise VerificationError(f"could not stop PostgreSQL (PID {server.pid}); scratch retained at {workspace}")


@contextmanager
def local_cluster(scratch, tools):
    workspace = Path(tempfile.mkdtemp(prefix="pgv-", dir=scratch))
    server = None
    try:
        if len(os.fsencode(workspace / SOCKET_NAME)) > 103:
            raise VerificationError("scratch path is too long for a PostgreSQL Unix socket")
        env = clean_env(workspace)
        data = workspace / "data"
        run_command([
            tools["initdb"], "-D", str(data), "--username", ROLE, "--auth", "trust",
            "--no-locale", "--encoding", "UTF8",
        ], env)
        logfile = workspace / "server.log"
        with logfile.open("wb") as log:
            server = subprocess.Popen([
                tools["postgres"], "-D", str(data), "-p", "5432",
                "-c", "listen_addresses=", "-c", f"unix_socket_directories={workspace}",
                "-c", "unix_socket_permissions=0700", "-c", "shared_buffers=16MB",
            ], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        connection = ["-h", str(workspace), "-p", "5432", "-U", ROLE]
        wait_until_ready(server, [tools["pg_isready"], *connection, "-d", DATABASE, "-q"], env, logfile)
        yield connection, env
    finally:
        if server is not None:
            stop_server(server, workspace)
        shutil.rmtree(workspace)


def check_expectations(expectations, psql, connection, env):
    argv = [
        psql, *connection, "-d", DATABASE, "-X", "-w", "-qAt", "-v", "ON_ERROR_STOP=1",
    ]
    query_env = {
        **env,
        "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=5000 "
        "-c search_path=pg_catalog",
    }

    def query(sql):
        return run_command([*argv, "-c", sql], query_env, timeout=CHECK_TIMEOUT).stdout.strip()

    results = []
    for expectation in expectations:
        schema, table, rows = expectation.schema, expectation.table, expectation.rows
        # The identifier grammar excludes quotes and all SQL metacharacters. Use exact case.
        exists = query(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{schema}' AND c.relname = '{table}' AND c.relkind = 'r')"
        )
        if exists not in ("t", "f"):
            raise VerificationError("unexpected table lookup result from psql")
        if exists == "f" or rows is None:
            results.append(CheckResult(expectation, exists == "t"))
            continue
        # ONLY keeps the count on this ordinary table; LIMIT detects excess without a full count.
        limit = rows + 1
        value = query(
            'SELECT pg_catalog.count(*) FROM '
            f'(SELECT 1 FROM ONLY "{schema}"."{table}" LIMIT {limit}) AS bounded_rows'
        )
        if not re.fullmatch(r"[0-9]+", value) or len(value) > len(str(limit)) or int(value) > limit:
            raise VerificationError("unexpected row count result from psql")
        count = int(value)
        results.append(CheckResult(expectation, True, count, count == limit))
    return results


def verify(dump, scratch, expectations=()):
    expectations = tuple(islice(expectations, MAX_EXPECTATIONS + 1))
    if len(expectations) > MAX_EXPECTATIONS:
        raise VerificationError(f"at most {MAX_EXPECTATIONS} expectations are allowed")
    if any(not isinstance(expectation, TableExpectation) for expectation in expectations):
        raise VerificationError("expected TableExpectation objects")
    dump = validate_dump(dump)
    scratch = validate_scratch(scratch)
    if os.geteuid() == 0:
        raise VerificationError("PostgreSQL must run as an unprivileged user, not root")
    tools = resolve_tools()
    with local_cluster(scratch, tools) as (connection, env):
        run_command([
            tools["pg_restore"], *connection, "-d", DATABASE, "--no-password", "--single-transaction",
            "--no-owner", "--no-privileges", "--", str(dump),
        ], env)
        return check_expectations(expectations, tools["psql"], connection, env)


def handle_termination(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="trusted pg_dump -Fc archive")
    parser.add_argument("--scratch-dir", type=Path, required=True, help="existing local scratch directory")
    args = parser.parse_args(argv)
    try:
        verify(args.dump, args.scratch_dir)
    except (VerificationError, OSError) as exc:
        print(f"verification error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("verification interrupted", file=sys.stderr)
        return 130
    print("PASS: dump restored successfully")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_termination)
    raise SystemExit(main())
