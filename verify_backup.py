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
from pathlib import Path

SOCKET_NAME = ".s.PGSQL.5432"
ROLE = "backup_verify"
DATABASE = "postgres"
TOOLS = ("initdb", "postgres", "pg_isready", "pg_restore")
COMMAND_TIMEOUT = 120


class VerificationError(Exception):
    """Invalid input or a failure to complete the verification."""


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


def verify(dump, scratch):
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
