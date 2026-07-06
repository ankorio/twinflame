"""M1.3 — match propagation through the full diff pipeline.

`Base` moved to a differently-named package between builds (an ordinary
refactor, unrelated to obfuscation/renaming). Plain package-based clustering
(Stage 1) puts it in a pool with zero candidates on the other side, so the
structural pass never even gets a chance to compare it to its true match —
it isn't a scoring failure, the candidate is simply never considered.

`Child` stays in the same package on both sides, so it matches confidently on
structure alone (no anchoring needed) and, via its declared superclass,
carries the type-graph edge propagation needs to recover `Base`'s match
across the pool boundary.
"""

from __future__ import annotations

import synthetic

from twinflame import api


def _child(descriptor, superclass):
    return synthetic.make_class(
        synthetic.ClassSpec(
            descriptor=descriptor,
            superclass=superclass,
            methods=synthetic.standard_methods(),
        )
    )


def _base(descriptor):
    return synthetic.make_class(
        synthetic.ClassSpec(descriptor=descriptor, methods=synthetic.standard_methods())
    )


def test_propagation_recovers_a_match_moved_to_a_different_package():
    child_l = _child("Lcom/vendor/app/Child;", "Lcom/vendor/util/Base;")
    base_l = _base("Lcom/vendor/util/Base;")
    child_r = _child("Lcom/vendor/app/Child;", "Lcom/vendor/newutil/Base;")
    base_r = _base("Lcom/vendor/newutil/Base;")

    matches = api.diff(
        [child_l, base_l],
        [child_r, base_r],
        threshold=0.8,
        optimizations={"cluster": True, "anchoring": False, "propagation": True},
    )
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in matches if m.is_paired}
    assert (child_l.descriptor, child_r.descriptor) in paired
    assert (base_l.descriptor, base_r.descriptor) in paired

    base_match = next(
        m for m in matches if m.is_paired and m.lhs.descriptor == base_l.descriptor
    )
    assert base_match.breakdown.get("propagated") == 1.0


def test_without_propagation_the_cross_package_base_is_missed():
    child_l = _child("Lcom/vendor/app/Child;", "Lcom/vendor/util/Base;")
    base_l = _base("Lcom/vendor/util/Base;")
    child_r = _child("Lcom/vendor/app/Child;", "Lcom/vendor/newutil/Base;")
    base_r = _base("Lcom/vendor/newutil/Base;")

    matches = api.diff(
        [child_l, base_l],
        [child_r, base_r],
        threshold=0.8,
        optimizations={"cluster": True, "anchoring": False, "propagation": False},
    )
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in matches if m.is_paired}
    assert (child_l.descriptor, child_r.descriptor) in paired
    assert (base_l.descriptor, base_r.descriptor) not in paired
    assert any(m.is_deleted and m.lhs.descriptor == base_l.descriptor for m in matches)
    assert any(m.is_added and m.rhs.descriptor == base_r.descriptor for m in matches)
