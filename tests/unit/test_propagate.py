"""Match propagation — type-graph cascade (apkdiff.propagate, M1.3)."""

from __future__ import annotations

import synthetic

from apkdiff.model import Match
from apkdiff.propagate import propagate_matches, referenced_descriptors


def _cls(descriptor, *, superclass=None, interfaces=(), fields=(), methods=None):
    return synthetic.make_class(
        synthetic.ClassSpec(
            descriptor=descriptor,
            superclass=superclass,
            interfaces=interfaces,
            fields=fields,
            methods=methods if methods is not None else synthetic.standard_methods(),
        )
    )


def _paired(lhs, rhs, distance=1.0):
    return Match(lhs=lhs, rhs=rhs, distance=distance)


def _deleted(lhs):
    return Match(lhs=lhs, rhs=None, distance=0.0)


def _added(rhs):
    return Match(lhs=None, rhs=rhs, distance=0.0)


# --- referenced_descriptors ---------------------------------------------


def test_referenced_descriptors_includes_superclass_and_interfaces():
    c = _cls(
        "Lcom/acme/Child;",
        superclass="Lcom/acme/Base;",
        interfaces=("Lcom/acme/Iface;",),
        methods=(),
    )
    assert referenced_descriptors(c) == ["Lcom/acme/Base;", "Lcom/acme/Iface;"]


def test_referenced_descriptors_extracts_field_types_and_unwraps_arrays():
    c = _cls(
        "Lcom/acme/Child;",
        fields=(
            synthetic.FieldSpec(name="f0", type_desc="Lcom/acme/Helper;"),
            synthetic.FieldSpec(name="f1", type_desc="I"),  # primitive, dropped
            synthetic.FieldSpec(name="f2", type_desc="[Lcom/acme/Item;"),  # array, unwrapped
        ),
        methods=(),
    )
    assert referenced_descriptors(c) == ["Lcom/acme/Helper;", "Lcom/acme/Item;"]


def test_referenced_descriptors_extracts_method_proto_types():
    c = _cls(
        "Lcom/acme/Child;",
        methods=(
            synthetic.MethodSpec(
                name="op",
                descriptor="(Lcom/acme/Arg;)Lcom/acme/Ret;",
                arg_count=1,
                return_type="Lcom/acme/Ret;",
            ),
        ),
    )
    assert referenced_descriptors(c) == ["Lcom/acme/Arg;", "Lcom/acme/Ret;"]


def test_referenced_descriptors_dedupes_and_preserves_order():
    c = _cls(
        "Lcom/acme/Child;",
        superclass="Lcom/acme/Base;",
        fields=(synthetic.FieldSpec(name="f0", type_desc="Lcom/acme/Base;"),),
        methods=(),
    )
    assert referenced_descriptors(c) == ["Lcom/acme/Base;"]


# --- propagate_matches ---------------------------------------------------


def test_propagates_superclass_match():
    child_l, child_r = _cls("Lcom/acme/ChildL;", superclass="Lcom/acme/BaseL;"), _cls(
        "Lcom/acme/ChildR;", superclass="Lcom/acme/BaseR;"
    )
    base_l, base_r = _cls("Lcom/acme/BaseL;"), _cls("Lcom/acme/BaseR;")

    matches = [
        _paired(child_l, child_r),
        _deleted(base_l),
        _added(base_r),
    ]
    out = propagate_matches(matches, threshold=0.8)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert (base_l.descriptor, base_r.descriptor) in paired
    new_match = next(m for m in out if m.is_paired and m.lhs.descriptor == base_l.descriptor)
    assert new_match.breakdown.get("propagated") == 1.0
    # No leftover deleted/added stub for the classes that just got claimed.
    assert not any(m.is_deleted and m.lhs.descriptor == base_l.descriptor for m in out)
    assert not any(m.is_added and m.rhs.descriptor == base_r.descriptor for m in out)


def test_propagates_single_interface_match():
    child_l = _cls("Lcom/acme/ChildL;", interfaces=("Lcom/acme/IfaceL;",))
    child_r = _cls("Lcom/acme/ChildR;", interfaces=("Lcom/acme/IfaceR;",))
    iface_l, iface_r = _cls("Lcom/acme/IfaceL;"), _cls("Lcom/acme/IfaceR;")

    matches = [_paired(child_l, child_r), _deleted(iface_l), _added(iface_r)]
    out = propagate_matches(matches, threshold=0.8)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert (iface_l.descriptor, iface_r.descriptor) in paired


def test_propagation_chains_across_multiple_rounds():
    """A -> B -> C: confirming A/A' should eventually also confirm C/C'."""
    a_l = _cls("La;", superclass="Lb;")
    a_r = _cls("La2;", superclass="Lb2;")
    b_l = _cls("Lb;", superclass="Lc;")
    b_r = _cls("Lb2;", superclass="Lc2;")
    c_l, c_r = _cls("Lc;"), _cls("Lc2;")

    matches = [
        _paired(a_l, a_r),
        _deleted(b_l),
        _added(b_r),
        _deleted(c_l),
        _added(c_r),
    ]
    out = propagate_matches(matches, threshold=0.8)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert ("Lb;", "Lb2;") in paired
    assert ("Lc;", "Lc2;") in paired


def test_does_not_touch_or_downgrade_an_existing_paired_match():
    child_l, child_r = _cls("Lchild;", superclass="Lbase;"), _cls("Lchild2;", superclass="Lbase2;")
    base_l, base_r = _cls("Lbase;"), _cls("Lbase2;")
    # base is *already* paired (perhaps wrongly) by an earlier stage.
    existing = _paired(base_l, base_r, distance=1.0)

    matches = [_paired(child_l, child_r), existing]
    out = propagate_matches(matches, threshold=0.8)
    assert existing in out
    # No duplicate/second match for base's descriptor was added.
    base_pairs = [m for m in out if m.is_paired and m.lhs.descriptor == "Lbase;"]
    assert len(base_pairs) == 1


def test_respects_threshold_and_leaves_dissimilar_candidates_unmatched():
    child_l, child_r = _cls("Lchild;", superclass="Lbase;"), _cls("Lchild2;", superclass="Lbase2;")
    base_l = _cls("Lbase;", methods=synthetic.standard_methods())
    base_r = _cls("Lbase2;", methods=())  # wildly different shape -> low score

    matches = [_paired(child_l, child_r), _deleted(base_l), _added(base_r)]
    out = propagate_matches(matches, threshold=0.99)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert ("Lbase;", "Lbase2;") not in paired
    # Original stubs survive untouched.
    assert any(m.is_deleted and m.lhs.descriptor == "Lbase;" for m in out)
    assert any(m.is_added and m.rhs.descriptor == "Lbase2;" for m in out)


def test_no_referenced_candidates_returns_matches_unchanged():
    child_l, child_r = _cls("Lchild;"), _cls("Lchild2;")
    matches = [_paired(child_l, child_r)]
    out = propagate_matches(matches, threshold=0.8)
    assert out == matches
