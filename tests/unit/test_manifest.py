from __future__ import annotations

from apkdiff.manifest import suggest_package_prefix
from apkdiff.model import ManifestInfo


def test_suggest_package_prefix_picks_common_root():
    m = ManifestInfo(
        package="com.acme.app",
        activities=("com.acme.app.MainActivity", "com.acme.app.ui.LoginActivity"),
        services=("com.acme.app.SyncService",),
        receivers=(),
        providers=(),
    )
    assert suggest_package_prefix(m) == "com.acme.app"


def test_suggest_package_prefix_expands_dotted_shorthand():
    m = ManifestInfo(
        package="com.acme.app",
        activities=(".MainActivity", ".ui.LoginActivity"),
        services=(),
        receivers=(),
        providers=(),
    )
    # Both expand under the package; common prefix is the package itself.
    assert suggest_package_prefix(m) == "com.acme.app"


def test_suggest_package_prefix_falls_back_to_package_when_no_components():
    m = ManifestInfo(package="com.acme.app", activities=(), services=(), receivers=(), providers=())
    assert suggest_package_prefix(m) == "com.acme.app"


def test_suggest_package_prefix_no_common_prefix_returns_package():
    m = ManifestInfo(
        package="fallback.pkg",
        activities=("com.x.A", "org.y.B"),
        services=(),
        receivers=(),
        providers=(),
    )
    assert suggest_package_prefix(m) == "fallback.pkg"


def test_suggest_package_prefix_drops_trailing_class_segment():
    # When all components are the literal same class, the "common prefix"
    # naively includes the class name; we strip it.
    m = ManifestInfo(
        package="",
        activities=("com.x.MainActivity",),
        services=(),
        receivers=(),
        providers=(),
    )
    assert suggest_package_prefix(m) == "com.x"
