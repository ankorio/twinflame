"""Self-diff regression gate.

Diffing a class set against itself must yield: every class matched at
distance == 1.0, zero deleted, zero added.
"""

from __future__ import annotations

import synthetic

from apkdiff import api


def test_self_diff_yields_only_perfect_matches():
    classes = synthetic.vendor_app(n_classes=8)
    matches = api.diff(classes, list(classes), threshold=0.8)
    paired = [m for m in matches if m.is_paired]
    deleted = [m for m in matches if m.is_deleted]
    added = [m for m in matches if m.is_added]
    assert len(paired) == 8
    assert deleted == []
    assert added == []
    for m in paired:
        assert m.distance == 1.0, f"non-perfect self-match: {m.distance:.4f} for {m.lhs.info}"


def test_self_diff_with_package_filter_scopes_input():
    classes = synthetic.vendor_app(package="com.acme.app", n_classes=5)
    classes += synthetic.vendor_app(package="com.acme.other", n_classes=3)
    filtered = api.filter(classes, {"package_filtering": "com.acme.app"})
    matches = api.diff(filtered, list(filtered), threshold=0.8)
    assert all(m.distance == 1.0 for m in matches if m.is_paired)
    assert len([m for m in matches if m.is_paired]) == 5
