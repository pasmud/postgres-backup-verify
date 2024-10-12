import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import verify_backup as backup

ARGS = ["synthetic.dump", "--scratch-dir", "scratch"]


def test_restore_only_text_regression(monkeypatch, capsys):
    verify = Mock(return_value=[])
    monkeypatch.setattr(backup, "verify", verify)

    assert backup.main(ARGS) == 0
    assert capsys.readouterr() == ("PASS: dump restored successfully\n", "")
    verify.assert_called_once_with(Path("synthetic.dump"), Path("scratch"), [])


def test_mixed_json_outcomes_and_cli_order(monkeypatch, capsys):
    empty = backup.TableExpectation("Demo", "Empty", 0)
    present = backup.TableExpectation("public", "items")
    lower = backup.TableExpectation("public", "items", 1)
    missing = backup.TableExpectation("public", "missing", 0)
    verify = Mock(return_value=[
        backup.CheckResult(empty, True, 0),
        backup.CheckResult(present, True),
        backup.CheckResult(lower, True, 2, True),
        backup.CheckResult(missing, False),
    ])
    monkeypatch.setattr(backup, "verify", verify)

    assert backup.main([
        *ARGS, "--json", "--expect-rows", "Demo.Empty=0", "--expect-table", "items",
        "--expect-rows", "items=1", "--expect-rows", "missing=0",
    ]) == 1

    out, err = capsys.readouterr()
    assert err == ""
    assert json.loads(out) == {
        "status": "fail",
        "checks": [
            {"status": "pass", "kind": "rows", "schema": "Demo", "table": "Empty",
             "table_exists": True, "expected_rows": 0, "observed_rows": 0,
             "observed_rows_is_lower_bound": False},
            {"status": "pass", "kind": "table", "schema": "public", "table": "items",
             "table_exists": True, "expected_rows": None, "observed_rows": None,
             "observed_rows_is_lower_bound": False},
            {"status": "fail", "kind": "rows", "schema": "public", "table": "items",
             "table_exists": True, "expected_rows": 1, "observed_rows": 2,
             "observed_rows_is_lower_bound": True},
            {"status": "fail", "kind": "rows", "schema": "public", "table": "missing",
             "table_exists": False, "expected_rows": 0, "observed_rows": None,
             "observed_rows_is_lower_bound": False},
        ],
    }
    verify.assert_called_once_with(Path("synthetic.dump"), Path("scratch"), [empty, present, lower, missing])


def test_text_explains_an_excess_count_without_claiming_an_exact_total(monkeypatch, capsys):
    expectation = backup.TableExpectation("public", "items", 0)
    monkeypatch.setattr(backup, "verify", Mock(return_value=[backup.CheckResult(expectation, True, 1, True)]))

    assert backup.main([*ARGS, "--expect-rows", "items=0"]) == 1
    assert capsys.readouterr() == (
        ("FAIL: public.items: expected 0 rows, found at least 1\n"
         "FAIL: dump restored; 1 of 1 expectations failed\n"),
        "",
    )


def test_missing_dump_is_a_json_error_not_a_mismatch(tmp_path, capsys):
    assert backup.main([str(tmp_path / "missing.dump"), "--scratch-dir", str(tmp_path), "--json"]) == 2
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["status"] == "error"
    assert report["checks"] == []
    assert "missing.dump" in report["error"]
    assert err == ""


def test_interruption_has_distinct_status(monkeypatch, capsys):
    monkeypatch.setattr(backup, "verify", Mock(side_effect=KeyboardInterrupt))
    assert backup.main([*ARGS, "--json"]) == 130
    out, err = capsys.readouterr()
    assert json.loads(out) == {"status": "interrupted", "checks": [], "error": "verification interrupted"}
    assert err == ""


@pytest.mark.parametrize(("flag", "spec"), [
    ("--expect-table", "public.items.extra"),
    ("--expect-table", "items;"),
    ("--expect-rows", "items"),
    ("--expect-rows", "items=-1"),
    ("--expect-rows", "items=100001"),
    ("--expect-rows", "items=" + "9" * 5000),
])
def test_invalid_expectation_is_a_usage_error(monkeypatch, capsys, flag, spec):
    verify = Mock()
    monkeypatch.setattr(backup, "verify", verify)

    with pytest.raises(SystemExit) as exc:
        backup.main([*ARGS, flag, spec])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert f"argument {flag}:" in err
    verify.assert_not_called()
