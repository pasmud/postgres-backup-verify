"""Materialize the checked-in offline corpus without importing the verifier.

Run from the repository root: python tests/generate_regression_vectors.py
"""

import json
from pathlib import Path

FLAGS = {
    "table": "--expect-table",
    "rows": "--expect-rows",
    "min_rows": "--expect-rows-at-least",
}
NAME_ERROR = (
    "schema and table names must be 1–63 ASCII letters, digits, or underscores "
    "and start with a letter or underscore"
)
COUNT_SYNTAX = "use [SCHEMA.]TABLE=COUNT with COUNT from 0 to 100000"
COUNT_RANGE = "expected rows must be an integer from 0 to 100000"
MODE_ERROR = "at_least must be a boolean and requires a row count when true"
LOOKUP_ERROR = "unexpected table lookup result from psql"
COUNT_ERROR = "unexpected row count result from psql"
VECTORS = {}


def add(slug, title, **vector):
    assert slug not in VECTORS
    VECTORS[slug] = {"title": title, **vector}


def report(slug, title, catalog, checks, *, reply=None, error=None):
    """Turn explicitly resolved names and unbounded catalog totals into an oracle."""
    args, queries, reports, lines = [], [], [], []
    for kind, name, target, *spelling in checks:
        schema, table = name.split(".")
        spec = table if schema == "public" else name
        if target is not None:
            spec += f"={target}"
        args.extend((FLAGS[kind], spelling[0] if spelling else spec))
        queries.append([name])

        relation = catalog.get(name, {})
        exists = relation.get("kind") == "r"
        observed, bounded = None, False
        passed = exists
        goal = f"expected {'at least ' if kind == 'min_rows' else ''}{target} rows"
        if not exists:
            detail = (
                "ordinary table not found"
                if target is None
                else f"{goal}; ordinary table not found"
            )
        elif kind == "table":
            detail = "ordinary table exists"
        else:
            total = relation["rows"]
            limit = target + (kind == "rows")
            queries.append([name, limit])
            # Classify using the uncapped ground truth, not the number psql returns.
            passed = total == target if kind == "rows" else total >= target
            observed = min(total, limit)
            bounded = total >= limit
            detail = f"{goal}, found {'at least ' if bounded else ''}{observed}"
        reports.append(
            {
                "status": "pass" if passed else "fail",
                "kind": kind,
                "schema": schema,
                "table": table,
                "table_exists": exists,
                "expected_rows": target,
                "observed_rows": observed,
                "observed_rows_is_lower_bound": bounded,
            }
        )
        lines.append(f"{'PASS' if passed else 'FAIL'}: {name}: {detail}")

    if error:
        queries = queries[: reply["call"]]
        outcome = {
            "exit": 2,
            "json": {"status": "error", "checks": [], "error": error},
            "text": [f"verification error: {error}"],
        }
    else:
        failures = sum(item["status"] == "fail" for item in reports)
        lines.append(
            f"FAIL: dump restored; {failures} of {len(checks)} expectations failed"
            if failures
            else "PASS: dump restored successfully"
        )
        outcome = {
            "exit": int(bool(failures)),
            "json": {"status": "fail" if failures else "pass", "checks": reports},
            "text": lines,
        }
    add(
        slug,
        title,
        args=args,
        catalog=catalog,
        queries=queries,
        **({"psql_reply": reply} if reply else {}),
        **outcome,
    )


def ordinary(rows, **extra):
    return {"kind": "r", "rows": rows, **extra}


def single(slug, title, kind, name, target, rows, *, spec=None, **kwargs):
    catalog = {} if rows is None else {name: ordinary(rows)}
    check = (kind, name, target) if spec is None else (kind, name, target, spec)
    report(slug, title, catalog, [check], **kwargs)


def row_cases():
    # fmt: off
    exact = [
        ("empty", "Exact zero distinguishes an empty table from a missing one", "public.outbox", 0, 0, None),
        ("unexpected-first-row", "Exact zero reads at most the first unexpected row", "public.outbox", 0, 1, None),
        ("one-row", "Exact one reports an uncapped singleton count", "public.settings", 1, 1, None),
        ("empty-shortfall", "A positive exact target reports an exact zero shortfall", "public.users", 5, 0, None),
        ("one-short", "An exact check reports the last count below target", "public.labels", 7, 6, None),
        ("limit-hit", "The first excess row is reported as a lower bound", "public.labels", 7, 8, None),
        ("heavy-surplus", "A large exact surplus is not exposed as an exact total", "public.logs", 7, 8_000_000, None),
        ("decimal-width", "An exact limit can have more digits than its target", "public.reports", 99, 100, None),
        ("maximum-match", "100000 passes an exact target without setting the bound bit", "public.events", 100000, 100000, None),
        ("maximum-short", "An exact maximum target can return a five-digit shortfall", "public.events", 100000, 99999, None),
        ("maximum-excess", "The largest exact target permits a 100001 query limit", "public.events", 100000, 100001, None),
        ("padded-count", "Six ASCII digits with leading zeros are a decimal target", "public.labels", 12, 12, "labels=000012"),
        ("qualified-missing", "An exact zero on an absent qualified table does not count", "Archive.Monthly", 0, None, None),
    ]
    minimum = [
        ("empty-zero", "Minimum zero on an empty table reaches the zero bound", "public.outbox", 0, 0, None),
        ("populated-zero", "Minimum zero on a populated table still uses LIMIT 0", "public.logs", 0, 9_000_000, None),
        ("missing-zero", "Minimum zero still fails when the table is absent", "public.outbox", 0, None, None),
        ("empty-positive", "A positive minimum observes zero on an empty table", "public.users", 1, 0, None),
        ("singleton", "Minimum one passes on a singleton and marks the bound", "public.settings", 1, 1, None),
        ("threshold-hit", "A minimum stops exactly at its nonzero threshold", "Sales.Orders", 31, 31, None),
        ("one-short", "A minimum just below threshold reports an exact shortfall", "Sales.Orders", 31, 30, None),
        ("heavy-surplus", "A minimum on a large relation reports only the threshold", "public.logs", 25, 8_000_000, None),
        ("maximum-hit", "100000 is both an accepted minimum and its query limit", "public.events", 100000, 100000, None),
        ("maximum-short", "The maximum minimum fails at 99999 without a bound bit", "public.events", 100000, 99999, None),
        ("maximum-surplus", "A minimum never probes 100001 even above the maximum", "public.events", 100000, 100001, None),
        ("padded-zero", "A padded minimum zero is reported as the integer zero", "public.labels", 0, 2, "labels=000000"),
    ]
    # fmt: on
    for prefix, kind, cases in (
        ("exact", "rows", exact),
        ("minimum", "min_rows", minimum),
    ):
        for slug, title, name, target, rows, spec in cases:
            single(f"{prefix}-{slug}", title, kind, name, target, rows, spec=spec)


def catalog_cases():
    # The boundary models relkind values and exact catalog identity, not SQL casts.
    longest = "archive_" + "x" * 55
    # fmt: off
    cases = [
        ("default-public", "An unqualified table check finds public without counting", "public.audit_log", {"public.audit_log": ordinary(4)}, None),
        ("explicit-public", "An explicitly named public table may be empty", "public.empty_queue", {"public.empty_queue": ordinary(0)}, "public.empty_queue"),
        ("only-other-schema", "An unqualified name does not search another schema", "public.invoices", {"billing.invoices": ordinary(3)}, None),
        ("wrong-schema-case", "A differently cased schema does not match", "sales.Orders", {"Sales.Orders": ordinary(3)}, None),
        ("wrong-table-case", "A differently cased table does not match", "Sales.orders", {"Sales.Orders": ordinary(3)}, None),
        ("exact-mixed-case", "Both components of a mixed-case name are preserved", "Sales.Orders", {"Sales.Orders": ordinary(3)}, None),
        ("view", "A view is not an ordinary table", "public.open_orders", {"public.open_orders": {"kind": "v"}}, None),
        ("materialized-view", "A materialized view is not an ordinary table", "public.order_totals", {"public.order_totals": {"kind": "m"}}, None),
        ("partitioned-parent", "A partitioned root is not an ordinary table", "public.events", {"public.events": {"kind": "p"}}, None),
        ("foreign-table", "A foreign table is not an ordinary table", "public.remote_events", {"public.remote_events": {"kind": "f"}}, None),
        ("sequence", "A sequence is not an ordinary table", "public.orders_id_seq", {"public.orders_id_seq": {"kind": "S"}}, None),
        ("index", "An index is not an ordinary table", "public.orders_pkey", {"public.orders_pkey": {"kind": "i"}}, None),
        ("underscore", "A single underscore is a supported table identifier", "public._", {"public._": ordinary(0)}, None),
        ("digit-suffix", "Digits after the first character are valid in both names", "staging2.Batch2024", {"staging2.Batch2024": ordinary(1)}, None),
        ("max-schema-length", "A 63-character schema is matched without truncation", f"{longest}.jobs", {f"{longest}.jobs": ordinary(6)}, None),
        ("max-table-length", "A 63-character table is matched without truncation", f"public.{longest}", {f"public.{longest}": ordinary(2)}, None),
    ]
    # fmt: on
    for slug, title, name, catalog, spec in cases:
        check = ("table", name, None) if spec is None else ("table", name, None, spec)
        report(f"table-{slug}", title, catalog, [check])


def combined_cases():
    # fmt: off
    report("restore-no-expectations", "A restore with no expectations issues no psql queries", {}, [])
    report("reserved-identifiers", "A row query quotes SQL keywords as identifiers",
           {"select.order": ordinary(4)}, [("rows", "select.order", 4)])
    report("leading-underscore-row-name", "A row query preserves leading underscores and case",
           {"_Import._Batch7": ordinary(8)}, [("min_rows", "_Import._Batch7", 7)])
    report("table-vs-view-same-name", "Lookup distinguishes a table and a view with the same name",
           {"public.orders": ordinary(12), "reporting.orders": {"kind": "v"}},
           [("rows", "reporting.orders", 12), ("rows", "public.orders", 12)])
    report("exact-vs-minimum-same-target", "The same restored count can fail exact and pass minimum",
           {"public.labels": ordinary(6)}, [("rows", "public.labels", 5), ("min_rows", "public.labels", 5)])
    report("duplicate-exact-targets", "Repeated exact flags retain distinct targets in CLI order",
           {"public.labels": ordinary(6)}, [("rows", "public.labels", 7), ("rows", "public.labels", 6)])
    report("duplicate-identical-check", "Identical expectations each produce a check and query",
           {"public.labels": ordinary(2)}, [("table", "public.labels", None)] * 2)
    report("independent-schema-counts", "Same-named tables in two schemas have independent counts",
           {"public.jobs": ordinary(3), "Archive.jobs": ordinary(18)},
           [("rows", "Archive.jobs", 18), ("rows", "public.jobs", 3, "public.jobs=3")])
    report("inheritance-parent-own-rows", "Empty parent does not count its inherited child rows",
           {"public.events": ordinary(0, descendant_rows=4), "history.events": ordinary(4)},
           [("rows", "public.events", 0), ("rows", "history.events", 4)])
    report("inheritance-minimum-shortfall", "Child rows cannot satisfy a parent's minimum",
           {"public.events": ordinary(2, descendant_rows=10)}, [("min_rows", "public.events", 3)])
    report("mixed-summary-order", "Mixed checks preserve order and count both failures",
           {"public.queue": ordinary(7), "public.audit": ordinary(1)},
           [("min_rows", "public.queue", 8), ("table", "public.audit", None),
            ("table", "public.missing", None), ("rows", "public.queue", 7)])
    # fmt: on


def invalid_inputs():
    # fmt: off
    usage = [
        ("empty-name", "An empty CLI table name fails identifier validation", "table", "", NAME_ERROR),
        ("empty-schema", "A leading dot does not stand for the default schema", "table", ".orders", NAME_ERROR),
        ("empty-table", "A trailing dot does not stand for a table name", "table", "sales.", NAME_ERROR),
        ("extra-component", "A qualified CLI name cannot have three components", "table", "db.sales.orders", NAME_ERROR),
        ("digit-initial-schema", "A schema name cannot start with a digit", "table", "2sales.orders", NAME_ERROR),
        ("digit-initial-table", "A table name cannot start with a digit", "table", "sales.2orders", NAME_ERROR),
        ("long-schema", "A 64-character schema is rejected rather than truncated", "table", "s" * 64 + ".jobs", NAME_ERROR),
        ("long-table", "A 64-character table is rejected rather than truncated", "table", "j" * 64, NAME_ERROR),
        ("quoted-name", "SQL double quotes are not part of a CLI identifier", "table", '"Orders"', NAME_ERROR),
        ("apostrophe", "An apostrophe is rejected in a catalog name", "table", "author's_notes", NAME_ERROR),
        ("hyphen", "A hyphen is not a valid identifier character", "table", "order-items", NAME_ERROR),
        ("internal-space", "An identifier cannot contain an internal space", "table", "order items", NAME_ERROR),
        ("trailing-newline", "An identifier cannot have a trailing newline", "table", "orders\n", NAME_ERROR),
        ("unicode-letter", "A non-ASCII letter is rejected in an identifier", "table", "données", NAME_ERROR),
        ("unicode-digit", "Non-ASCII digits are rejected after an identifier prefix", "table", "orders２", NAME_ERROR),
        ("dollar-sign", "A dollar sign is outside the supported identifier grammar", "table", "orders$2024", NAME_ERROR),
        ("count-with-table-flag", "The table-only flag rejects an equals suffix", "table", "orders=3", NAME_ERROR),
        ("row-name-invalid", "A valid exact count does not bypass name validation", "rows", "sales.bad-name=1", NAME_ERROR),
        ("minimum-name-invalid", "A valid minimum does not bypass name validation", "min_rows", "sales.=1", NAME_ERROR),
        ("missing-count-separator", "An exact row flag requires an equals separator", "rows", "orders", COUNT_SYNTAX),
        ("empty-count", "An equals separator requires at least one decimal digit", "min_rows", "orders=", COUNT_SYNTAX),
        ("negative-count", "A minus sign is not part of the CLI count grammar", "rows", "orders=-1", COUNT_SYNTAX),
        ("positive-sign", "A plus sign is not part of the CLI count grammar", "min_rows", "orders=+1", COUNT_SYNTAX),
        ("fraction", "A decimal point does not denote an integer row target", "rows", "orders=1.0", COUNT_SYNTAX),
        ("exponent", "Scientific notation is not a CLI row target", "min_rows", "orders=1e3", COUNT_SYNTAX),
        ("numeric-separator", "Underscores are not accepted inside a CLI row target", "rows", "orders=1_000", COUNT_SYNTAX),
        ("unicode-count", "Unicode decimal digits are not CLI row targets", "min_rows", "orders=١٢", COUNT_SYNTAX),
        ("leading-count-space", "CLI row targets cannot start with whitespace", "rows", "orders= 4", COUNT_SYNTAX),
        ("trailing-count-space", "CLI row targets cannot end with whitespace", "min_rows", "orders=4\t", COUNT_SYNTAX),
        ("double-equals", "A second equals sign makes the row target invalid", "rows", "orders==4", COUNT_SYNTAX),
        ("seven-digit-padding", "Seven digits are rejected even if they represent zero", "min_rows", "orders=0000000", COUNT_SYNTAX),
        ("above-exact-cap", "A six-digit exact target can exceed the numeric cap", "rows", "orders=100001", COUNT_RANGE),
        ("above-minimum-cap", "A six-digit minimum target can exceed the numeric cap", "min_rows", "orders=999999", COUNT_RANGE),
    ]
    # fmt: on
    for slug, title, kind, spec, error in usage:
        flag = FLAGS[kind]
        add(
            f"cli-{slug}",
            title,
            args=[flag, spec],
            usage_error=f"argument {flag}: {error}",
        )

    # fmt: off
    api = [
        ("null-schema", "A direct API schema cannot be null", [None, "orders"], NAME_ERROR),
        ("integer-table", "A direct API table cannot be an integer", ["public", 42], NAME_ERROR),
        ("list-schema", "A direct API schema cannot be a list of names", [["public"], "orders"], NAME_ERROR),
        ("qualified-table", "The API table parameter cannot include a schema", ["public", "sales.orders"], NAME_ERROR),
        ("invalid-table-char", "The API rejects slash inside a table identifier", ["public", "orders/2024"], NAME_ERROR),
        ("boolean-rows", "The API does not treat true as a row target of one", ["public", "orders", True], COUNT_RANGE),
        ("integral-float", "The API rejects a float even if its value is integral", ["public", "orders", 2.0], COUNT_RANGE),
        ("string-rows", "Only the CLI coerces a decimal string to a row target", ["public", "orders", "2"], COUNT_RANGE),
        ("object-rows", "A structured object is not an API row target", ["public", "orders", {"value": 2}], COUNT_RANGE),
        ("negative-rows", "The API rejects a negative integer row target", ["public", "orders", -1], COUNT_RANGE),
        ("above-row-cap", "The API applies the cap even without CLI parsing", ["public", "orders", 100001], COUNT_RANGE),
        ("missing-minimum-rows", "The API minimum mode requires a row target", ["public", "orders", None, True], MODE_ERROR),
        ("integer-minimum-mode", "The API does not coerce numeric zero to exact mode", ["public", "orders", 3, 0], MODE_ERROR),
        ("string-minimum-mode", "The API does not coerce a string to minimum mode", ["public", "orders", 3, "true"], MODE_ERROR),
        ("null-minimum-mode", "The API does not coerce null to a boolean mode", ["public", "orders", 3, None], MODE_ERROR),
    ]
    # fmt: on
    for slug, title, args, error in api:
        add(f"api-{slug}", title, api_args=args, error=error)


def psql_cases():
    # fmt: off
    cases = [
        ("lookup-empty", "Empty lookup stdout is an error, not a missing table", 1, {"stdout": ""}, LOOKUP_ERROR),
        ("lookup-long-boolean", "Lookup accepts psql t or f, not spelled-out true", 1, {"stdout": "true\n"}, LOOKUP_ERROR),
        ("lookup-uppercase", "Uppercase T is not a valid psql lookup result", 1, {"stdout": "T\n"}, LOOKUP_ERROR),
        ("lookup-two-tuples", "Lookup rejects multiple tuples even if each is boolean", 1, {"stdout": "t\nf\n"}, LOOKUP_ERROR),
        ("lookup-numeric", "Lookup rejects numeric truth values", 1, {"stdout": "1\n"}, LOOKUP_ERROR),
        ("lookup-trim", "Lookup trims outer whitespace around a valid result", 1, {"stdout": " \tt\r\n"}, None),
        ("count-empty", "An empty count is an error, not an observed zero", 2, {"stdout": "\n"}, COUNT_ERROR),
        ("count-negative", "A negative psql count is an operational error", 2, {"stdout": "-1\n"}, COUNT_ERROR),
        ("count-explicit-plus", "The psql count cannot have a plus sign", 2, {"stdout": "+2\n"}, COUNT_ERROR),
        ("count-fraction", "The psql count cannot be formatted as a fraction", 2, {"stdout": "2.0\n"}, COUNT_ERROR),
        ("count-two-tuples", "The psql count must be one tuple, not two lines", 2, {"stdout": "2\n2\n"}, COUNT_ERROR),
        ("count-multiple-columns", "The psql count must be a single scalar column", 2, {"stdout": "2|2\n"}, COUNT_ERROR),
        ("count-column-heading", "Aligned psql output with a count heading is invalid", 2, {"stdout": " count\n-------\n 2\n"}, COUNT_ERROR),
        ("count-unicode", "A psql count must use ASCII digits", 2, {"stdout": "٢\n"}, COUNT_ERROR),
        ("count-too-wide", "Extra leading zeros can exceed the query limit's width", 2, {"stdout": "002\n"}, COUNT_ERROR),
        ("count-over-limit", "A count above the limit can still have the allowed width", 2, {"stdout": "11\n"}, COUNT_ERROR),
        ("count-padded-within-width", "Psql count padding is accepted within the limit width", 2, {"stdout": "02\n"}, None),
        ("count-trim", "Psql count parsing strips outer whitespace", 2, {"stdout": "\t2 \r\n"}, None),
        ("count-stderr-on-success", "Successful psql notices do not corrupt a valid count", 2, {"stderr": "NOTICE: synthetic notice\n"}, None),
        ("lookup-process-error", "A nonzero lookup exit prefers stderr over valid stdout", 1,
         {"exit": 2, "stdout": "t\n", "stderr": "  connection unavailable\n"}, "psql exited 2: connection unavailable"),
        ("count-process-stdout-fallback", "A failed count falls back to stdout when stderr is blank", 2,
         {"exit": 3, "stdout": " query canceled\n", "stderr": " \n"}, "psql exited 3: query canceled"),
        ("count-process-no-diagnostics", "A failed count with no output has a stable diagnostic", 2,
         {"exit": 1, "stdout": "", "stderr": ""}, "psql exited 1: no diagnostics"),
        ("lookup-process-timeout", "A timed-out lookup reports the psql command deadline", 1,
         {"timeout": True}, "psql timed out after 10s"),
    ]
    # fmt: on
    for slug, title, call, response, error in cases:
        single(
            f"psql-{slug}",
            title,
            "rows",
            "public.labels",
            9,
            2,
            reply={"call": call, **response},
            error=error,
        )

    report(
        "psql-late-count-error",
        "A later psql error discards completed checks and stops",
        {"public.labels": ordinary(2), "public.archive": ordinary(6)},
        [
            ("rows", "public.labels", 2),
            ("min_rows", "public.archive", 4),
            ("table", "public.labels", None),
        ],
        reply={"call": 4, "stdout": "5\n"},
        error=COUNT_ERROR,
    )


def main():
    row_cases()
    catalog_cases()
    combined_cases()
    invalid_inputs()
    psql_cases()
    titles = [vector["title"] for vector in VECTORS.values()]
    assert len(titles) == len(set(titles))
    assert all(0 < len(title) < 72 and title.isprintable() for title in titles)

    directory = Path(__file__).with_name("regression_vectors")
    directory.mkdir(exist_ok=True)
    for slug, vector in VECTORS.items():
        (directory / f"{slug}.json").write_text(
            json.dumps(vector, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
