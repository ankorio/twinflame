"""eval/cli.py's ground-truth package scoping (M3.2)."""

from __future__ import annotations

from eval.cli import _in_package


def test_matches_exact_package_and_subpackages():
    assert _in_package("com.acme.app.MainActivity", "com.acme.app")
    assert _in_package("com.acme.app.ui.Screen", "com.acme.app")


def test_does_not_match_sibling_package_with_shared_prefix():
    # Regression: a naive str.startswith(prefix) without the dot boundary
    # would wrongly match "com.acme.appwidget.Foo" against prefix "com.acme.app".
    assert not _in_package("com.acme.appwidget.Foo", "com.acme.app")


def test_does_not_match_unrelated_package():
    assert not _in_package("androidx.compose.runtime.Composer", "com.acme.app")
