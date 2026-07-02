"""ProGuard mapping.txt ground-truth parser (eval/mapping.py, M3.2)."""

from __future__ import annotations

from eval.mapping import parse_class_mapping


def test_parses_class_lines_ignores_members_and_comments():
    text = """\
# apkdiff cross-version deobfuscation map
# low-confidence (0.85)
com.acme.Login -> a.a:
    int fieldX -> a
    void method(int) -> a
com.acme.other.Session -> a.b:
"""
    assert parse_class_mapping(text) == {
        "com.acme.Login": "a.a",
        "com.acme.other.Session": "a.b",
    }


def test_ignores_blank_and_malformed_lines():
    text = "\nnot a class line\ncom.acme.Foo -> a.c:\n"
    assert parse_class_mapping(text) == {"com.acme.Foo": "a.c"}


def test_identity_lines_are_kept():
    # R8 emits a line even for classes it doesn't rename.
    text = "com.acme.Kept -> com.acme.Kept:\n"
    assert parse_class_mapping(text) == {"com.acme.Kept": "com.acme.Kept"}


def test_empty_text_yields_empty_mapping():
    assert parse_class_mapping("") == {}
