"""End-to-end change-classifier oracle.

Runs the full `api.diff -> changes.change_set` pipeline over a baseline synthetic
app and a mutated copy with *labeled* mutations, and asserts the classifier
reports exactly the injected changes (kind + localized deltas) and nothing else.
This is the ground-truth regression the roadmap's evaluation section calls for
(synthetic mutation for CI).
"""

from __future__ import annotations

import sys
from pathlib import Path

from twinflame import api
from twinflame.changes import change_set

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import mutate as mut  # noqa: E402

PKG = "com.acme.app"
OPTS = {"jobs": 1}  # defaults otherwise (cluster on, anchoring on, min_inst 2)


def _run():
    baseline, mutated, expected = mut.mutation_scenario(PKG)
    matches = api.diff(baseline, mutated, threshold=0.8, optimizations=OPTS)
    changes = change_set(matches, dev_package=PKG)
    by_desc = {}
    for c in changes:
        by_desc[c.lhs or c.rhs] = c
    return by_desc, expected


def test_every_class_gets_exactly_its_expected_kind():
    by_desc, expected = _run()
    for desc, exp in expected.items():
        assert desc in by_desc, f"{desc} missing from change report"
        assert by_desc[desc].kind == exp.kind, (
            f"{desc}: expected {exp.kind}, got {by_desc[desc].kind} "
            f"(summary: {by_desc[desc].summary})"
        )


def test_no_spurious_extra_classes_reported():
    by_desc, expected = _run()
    # Exactly the 9 classes in the scenario, no phantom added/removed pairs.
    assert set(by_desc) == set(expected)


def test_framework_call_deltas_are_localized_correctly():
    by_desc, expected = _run()
    for desc, exp in expected.items():
        c = by_desc[desc]
        if exp.kind != "modified" or c.delta is None:
            continue
        assert exp.calls_added <= set(c.delta.calls_added), desc
        assert exp.calls_removed <= set(c.delta.calls_removed), desc
        assert exp.strings_added <= set(c.delta.strings_added), desc


def test_added_class_new_behavior_visible():
    by_desc, expected = _run()
    added = by_desc[f"L{PKG.replace('.', '/')}/C8;"]
    assert added.kind == "added"
    assert added.rhs is not None and added.lhs is None


def test_removed_class_reported_once():
    by_desc, _ = _run()
    removed = by_desc[f"L{PKG.replace('.', '/')}/C7;"]
    assert removed.kind == "removed"
    assert removed.lhs is not None and removed.rhs is None


def test_method_add_delete_localized_and_force_modified():
    by_desc, _ = _run()
    base = f"L{PKG.replace('.', '/')}"

    c3 = by_desc[f"{base}/C3;"]  # gained a method
    assert c3.kind == "modified"
    assert any(md.status == "added" and md.name == "added" for md in c3.method_deltas)

    c4 = by_desc[f"{base}/C4;"]  # lost a no-call method: modified purely structurally
    assert c4.kind == "modified"
    assert any(md.status == "deleted" and md.name == "helper" for md in c4.method_deltas)
    # its delta carries no framework-call/string change — the method delete alone
    # is what makes it "modified".
    assert not (c4.delta and c4.delta.is_semantic)


def test_cosmetic_class_has_no_semantic_delta():
    by_desc, _ = _run()
    c5 = by_desc[f"L{PKG.replace('.', '/')}/C5;"]
    assert c5.kind == "cosmetic"
    assert c5.delta is None
    assert c5.match_distance < 1.0  # structure did move (that's why it's not "unchanged")
