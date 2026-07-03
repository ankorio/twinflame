"""ProGuard mapping.txt ground-truth parser (eval/mapping.py, M3.2)."""

from __future__ import annotations

from eval.mapping import is_synthetic_like, parse_class_mapping


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


# --- is_synthetic_like -----------------------------------------------------


def test_flags_external_synthetic_lambda():
    assert is_synthetic_like("com.acme.ScreenKt$$ExternalSyntheticLambda12")


def test_flags_dollar_dollar_lambda():
    assert is_synthetic_like("com.acme.-$$Lambda$Foo$abc123")


def test_does_not_flag_plain_anonymous_inner_class():
    # Regression: these commonly hold real logic (e.g. Kotlin coroutine
    # continuation bodies) per the CalculatorM3 diagnosis — must not be
    # excluded from grading just because they have a numeric suffix.
    assert not is_synthetic_like("com.acme.ScanViewModel$ensureModelLoaded$1")
    assert not is_synthetic_like("com.acme.Screen$Composable$1$1")


def test_does_not_flag_ordinary_class():
    assert not is_synthetic_like("com.acme.MainActivity")
