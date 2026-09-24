"""Plain-assertion tests for workflow path auto-fix override parsing."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workflow_path_autofix import (  # noqa: E402
    parse_override_line,
    save_override_rows,
    validate_override_rows,
)


def test_supported_delimiters():
    assert parse_override_line("source target") == ("source", "target")
    assert parse_override_line("source   target") == ("source", "target")
    assert parse_override_line("source\ttarget") == ("source", "target")
    assert parse_override_line("source \t  target") == ("source", "target")


def test_malformed_lines():
    assert parse_override_line("source") is None
    assert parse_override_line("source target extra") is None
    assert parse_override_line(" source target") is None
    assert parse_override_line("source target ") is None
    assert parse_override_line("source\ntarget") is None


def test_override_row_validation():
    rows = [{"search": "old\\model", "replacement": "new/model"}]
    assert validate_override_rows(rows) == [("old\\model", "new/model")]

    invalid_rows = [
        None,
        {},
        {"search": "", "replacement": "target"},
        {"search": "has space", "replacement": "target"},
        {"search": "source", "replacement": "has\ttab"},
        {"search": "source", "replacement": "has\nnewline"},
    ]
    for row in invalid_rows:
        try:
            validate_override_rows([row])
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid override row: {row!r}")


def test_save_override_rows():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "overrides.txt")
        save_override_rows(path, [
            {"search": "first", "replacement": "second"},
            {"search": "old/path", "replacement": "new\\path"},
        ])
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "first\tsecond\n" in content
        assert "old/path\tnew\\path\n" in content


if __name__ == "__main__":
    test_supported_delimiters()
    test_malformed_lines()
    test_override_row_validation()
    test_save_override_rows()
    print("OK — all workflow path auto-fix parsing checks passed")