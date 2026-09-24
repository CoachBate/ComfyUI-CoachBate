"""Helpers for workflow model path auto-fix overrides."""

import os
import re
import tempfile


_OVERRIDE_PATTERN = re.compile(r"(\S+)[ \t]+(\S+)")
_OVERRIDE_HEADER = """# Ordered CoachBate workflow model path overrides.
# Format: search and replacement separated by one or more spaces and/or tabs.
# Search and replacement names cannot contain spaces or tabs.
# Blank lines and lines beginning with # are ignored.
# These rules are only applied to model widgets, then validated against that widget's local model choices.

"""
_MAX_OVERRIDES = 1000
_MAX_NAME_LENGTH = 4096


def parse_override_line(line):
    """Return a source/target pair separated by spaces and/or tabs."""
    match = _OVERRIDE_PATTERN.fullmatch(line)
    return match.groups() if match else None


def validate_override_rows(rows):
    """Validate JSON-style override rows and return normalized string pairs."""
    if not isinstance(rows, list):
        raise ValueError("overrides must be a list")
    if len(rows) > _MAX_OVERRIDES:
        raise ValueError(f"at most {_MAX_OVERRIDES} overrides are allowed")

    normalized = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"override {index} must be an object")
        search = row.get("search")
        replacement = row.get("replacement")
        if not isinstance(search, str) or not isinstance(replacement, str):
            raise ValueError(f"override {index} source and target must be strings")
        if not search or not replacement:
            raise ValueError(f"override {index} source and target are required")
        if len(search) > _MAX_NAME_LENGTH or len(replacement) > _MAX_NAME_LENGTH:
            raise ValueError(f"override {index} source or target is too long")
        if any(character.isspace() for character in search + replacement):
            raise ValueError(f"override {index} source and target cannot contain whitespace")
        normalized.append((search, replacement))
    return normalized


def save_override_rows(path: str, rows):
    """Atomically replace an override file with validated rows."""
    normalized = validate_override_rows(rows)
    directory = os.path.dirname(path) or "."
    fd, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_OVERRIDE_HEADER)
            for search, replacement in normalized:
                fh.write(f"{search}\t{replacement}\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    return normalized