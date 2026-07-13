"""Output-writing helper: create missing parent directories rather than crash.

Regression for the usability bug where `-o`/`--components-file`/
`--deobfuscation-map` into a not-yet-existing folder crashed the CLI.
"""

from __future__ import annotations

import pytest

from twinflame.cli import _write_output


def test_write_output_creates_missing_parents(tmp_path):
    dest = tmp_path / "a" / "b" / "c" / "report.json"
    _write_output(dest, "{}")
    assert dest.read_text() == "{}"


def test_write_output_into_existing_dir(tmp_path):
    dest = tmp_path / "report.csv"
    _write_output(dest, "x,y\n")
    assert dest.read_text() == "x,y\n"


def test_write_output_overwrites(tmp_path):
    dest = tmp_path / "sub" / "out.txt"
    _write_output(dest, "first")
    _write_output(dest, "second")
    assert dest.read_text() == "second"
