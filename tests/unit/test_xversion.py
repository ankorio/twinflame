from __future__ import annotations

from eval.xversion import build_cross_version_truth


def test_join_keeps_only_originals_present_in_both_builds():
    mx = {"org.foo.Bar": "a.b", "org.foo.Gone": "a.c"}      # Gone: removed in Y
    my = {"org.foo.Bar": "c.d", "org.foo.New": "c.e"}       # New: added in Y
    truth = build_cross_version_truth(mx, my, package=None, exclude_synthetic_like=True)
    # Only Bar is in both -> its obfuscated names correspond across versions.
    assert truth == {"a.b": "c.d"}


def test_join_scopes_to_package_on_original_name():
    mx = {"org.foo.Bar": "a.b", "com.other.Baz": "a.c"}
    my = {"org.foo.Bar": "c.d", "com.other.Baz": "c.e"}
    truth = build_cross_version_truth(mx, my, package="org.foo", exclude_synthetic_like=True)
    assert truth == {"a.b": "c.d"}


def test_join_excludes_synthetic_like_by_original_name():
    mx = {"org.foo.Bar$$ExternalSyntheticLambda0": "a.b", "org.foo.Real": "a.c"}
    my = {"org.foo.Bar$$ExternalSyntheticLambda0": "c.d", "org.foo.Real": "c.e"}
    truth = build_cross_version_truth(mx, my, package=None, exclude_synthetic_like=True)
    assert truth == {"a.c": "c.e"}  # synthetic-like dropped, Real kept


def test_join_can_include_synthetic_like():
    mx = {"org.foo.Bar$$ExternalSyntheticLambda0": "a.b"}
    my = {"org.foo.Bar$$ExternalSyntheticLambda0": "c.d"}
    truth = build_cross_version_truth(mx, my, package=None, exclude_synthetic_like=False)
    assert truth == {"a.b": "c.d"}


def test_join_excludes_r8_removed_phantoms():
    # R8 deleted Gone on the Y side (dead-code shrink) -> not in the APK.
    mx = {"org.foo.Gone": "a.b", "org.foo.Real": "a.c"}
    my = {"org.foo.Gone": "R8$$REMOVED$$CLASS$$7", "org.foo.Real": "c.d"}
    truth = build_cross_version_truth(mx, my, package=None, exclude_synthetic_like=True)
    assert truth == {"a.c": "c.d"}  # phantom dropped, Real kept
