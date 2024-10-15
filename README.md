# postgres-backup-verify

Restore a trusted PostgreSQL custom-format archive into a new local cluster,
then verify that expected tables and row counts survived the restore. The tool
does not take a connection string. It creates its own private Unix socket and
deletes its temporary cluster when finished.

## Quickstart

Run as an ordinary user on Linux with Python 3.11+ and the PostgreSQL server
and client binaries on `PATH`. If `pg_config` is installed and the tools are
not otherwise on `PATH`, try `export PATH="$(pg_config --bindir):$PATH"`.
Use a PostgreSQL version compatible with the source dump. Choose an existing
scratch directory with enough free space for both the restored data and indexes.
Optionally set `--min-free-mib 512` (or another nonnegative number of MiB) to
fail before starting PostgreSQL if the scratch filesystem has less free space.
The default, `0`, disables the check. This is a point-in-time check, not a quota
or an estimate of the restored size.

```sh
python3 verify_backup.py sample.dump --scratch-dir /tmp \
  --expect-table public.labels --expect-rows public.labels=2 --json
```

This example expects an archive produced by `pg_dump -Fc` that contains an
ordinary table `public.labels` with two rows. The [integration test](tests/test_postgres.py)
creates exactly that archive from synthetic data in a separate disposable
cluster. Use `--expect-rows-at-least public.labels=1` when a table is expected to
grow across backups. All three expectation flags can be repeated and mixed, up
to 32 checks per run. Unqualified names refer to `public`; names are matched
using exact case. If you omit expectations, the tool checks only whether the
restore succeeded.

| Exit | Meaning |
|---|---|
| `0` | Restore and all requested checks passed. |
| `1` | Restore succeeded but at least one check failed. |
| `2` | Invalid input or an operational failure. |
| `130` | Interrupted. |

Row targets are capped at 100,000. Exact checks stop at one row beyond the
target; minimum checks stop at the target itself, so a minimum of zero still
requires the table to exist but does not read any rows. In JSON, minimum checks
have `kind: "min_rows"` and `expected_rows` is the minimum. For either kind of
row check, `observed_rows_is_lower_bound` is true when the count reaches the
query limit; otherwise an observed count is exact. Ground-truth expectations
are supplied by the caller. Views and partitions are outside the table-check
API, and indexes, grants, application behavior, and data contents are outside
the check scope.

## Scope and verification

Use trusted archives only: `pg_restore` executes SQL from the archive under
the local OS account. The disposable cluster isolates database state, not
code execution. Database ownership and privileges are not restored. The
temporary files are removed after shutdown; a shutdown failure may leave the
workspace path in the error for local cleanup.

```sh
python3 -m pytest -q
REQUIRE_POSTGRES_TESTS=1 python3 -m pytest -q tests/test_postgres.py
```

The first command runs unit tests and the PostgreSQL integration if the tools
are present. The second makes missing PostgreSQL tools an error. GitHub CI runs
the real restore test with the required binaries installed.

[MIT license](LICENSE).
