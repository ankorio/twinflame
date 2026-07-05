"""Match propagation — type-graph cascade + call-site anchor (apkdiff.propagate, M1.3/M1.4)."""

from __future__ import annotations

import synthetic

from apkdiff.model import Match, MethodMatch
from apkdiff.propagate import instantiation_pairs, propagate_matches, referenced_descriptors


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


def _method(name, instantiates=()):
    return synthetic.make_method(synthetic.MethodSpec(name=name, instantiates=instantiates))


def _paired(lhs, rhs, distance=1.0, method_matches=()):
    return Match(lhs=lhs, rhs=rhs, distance=distance, method_matches=method_matches)


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


# --- instantiation_pairs (M1.4) -------------------------------------------


def test_instantiation_pairs_zips_by_position():
    caller_l = _method("build", instantiates=("Lx;", "Ly;", "Lz;"))
    caller_r = _method("build", instantiates=("Lx2;", "Ly2;", "Lz2;"))
    mm = MethodMatch(lhs=caller_l, rhs=caller_r, score=1.0, status="matched")
    m = _paired(_cls("Lcaller;"), _cls("Lcaller2;"), method_matches=(mm,))
    assert instantiation_pairs(m) == [("Lx;", "Lx2;"), ("Ly;", "Ly2;"), ("Lz;", "Lz2;")]


def test_instantiation_pairs_truncates_to_shorter_side():
    caller_l = _method("build", instantiates=("Lx;", "Ly;", "Lz;"))
    caller_r = _method("build", instantiates=("Lx2;",))  # one instantiation optimized away
    mm = MethodMatch(lhs=caller_l, rhs=caller_r, score=0.9, status="modified")
    m = _paired(_cls("Lcaller;"), _cls("Lcaller2;"), method_matches=(mm,))
    assert instantiation_pairs(m) == [("Lx;", "Lx2;")]


def test_instantiation_pairs_skips_added_and_deleted_methods():
    added = MethodMatch(lhs=None, rhs=_method("a", instantiates=("Lz;",)), score=0.0, status="added")
    deleted = MethodMatch(lhs=_method("b", instantiates=("Ly;",)), rhs=None, score=0.0, status="deleted")
    m = _paired(_cls("Lcaller;"), _cls("Lcaller2;"), method_matches=(added, deleted))
    assert instantiation_pairs(m) == []


# --- propagate_matches: call-site anchor (M1.4) ---------------------------


def test_propagates_via_matched_callers_instantiation_site():
    caller_l = _method("build", instantiates=("Lcom/acme/HelperL;",))
    caller_r = _method("build", instantiates=("Lcom/acme/HelperR;",))
    mm = MethodMatch(lhs=caller_l, rhs=caller_r, score=1.0, status="matched")
    caller_match = _paired(_cls("Lcom/acme/CallerL;"), _cls("Lcom/acme/CallerR;"), method_matches=(mm,))

    helper_l = _cls("Lcom/acme/HelperL;")
    helper_r = _cls("Lcom/acme/HelperR;")

    matches = [caller_match, _deleted(helper_l), _added(helper_r)]
    out = propagate_matches(matches, threshold=0.8)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert (helper_l.descriptor, helper_r.descriptor) in paired
    new_match = next(
        m for m in out if m.is_paired and m.lhs.descriptor == helper_l.descriptor
    )
    assert new_match.breakdown.get("call_site_anchored") == 1.0


def test_call_site_anchor_uses_a_lower_floor_than_the_structural_threshold():
    """Regression: the first cut gated call-site-anchored pairs by the same
    structural `threshold` as open-candidate-search propagation. On the real
    CalculatorM3 corpus, verified-correct call-site pairs (e.g. a Kotlin
    Companion object, nearly empty by construction) scored as low as 0.24 —
    below the default 0.8 threshold, so every one of them was silently
    rejected. The positional correlation *is* the confidence signal for this
    mechanism; CALL_SITE_MIN_CONFIDENCE is a much lower sanity floor, not a
    structural-confidence gate.
    """
    caller_l = _method("build", instantiates=("Lcom/acme/HelperL;",))
    caller_r = _method("build", instantiates=("Lcom/acme/HelperR;",))
    mm = MethodMatch(lhs=caller_l, rhs=caller_r, score=1.0, status="matched")
    caller_match = _paired(_cls("Lcom/acme/CallerL;"), _cls("Lcom/acme/CallerR;"), method_matches=(mm,))

    # Structurally dissimilar (e.g. an empty-vs-populated Companion object)
    # but a real, correct call-site-anchored pair — must still be accepted
    # even though it would fail the default (or even a very high) threshold.
    helper_l = _cls("Lcom/acme/HelperL;", methods=synthetic.standard_methods())
    helper_r = _cls("Lcom/acme/HelperR;", methods=())

    matches = [caller_match, _deleted(helper_l), _added(helper_r)]
    out = propagate_matches(matches, threshold=0.99)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert (helper_l.descriptor, helper_r.descriptor) in paired


def test_call_site_anchor_rejects_a_genuinely_nonsensical_pairing():
    caller_l = _method("build", instantiates=("Lcom/acme/HelperL;",))
    caller_r = _method("build", instantiates=("Lcom/acme/HelperR;",))
    mm = MethodMatch(lhs=caller_l, rhs=caller_r, score=1.0, status="matched")
    caller_match = _paired(_cls("Lcom/acme/CallerL;"), _cls("Lcom/acme/CallerR;"), method_matches=(mm,))

    helper_l = _cls("Lcom/acme/HelperL;", methods=synthetic.standard_methods())
    helper_r = _cls(
        "Lcom/acme/HelperR;",
        methods=(
            synthetic.MethodSpec(
                name="totallyDifferent", descriptor="(IIIII)J", arg_count=5, return_type="J",
                bytecode=bytes(range(1, 11)), instr_count=10,
            ),
        ),
        fields=(
            synthetic.FieldSpec(name="x", type_desc="Ljava/lang/String;"),
            synthetic.FieldSpec(name="y", type_desc="J"),
        ),
    )

    matches = [caller_match, _deleted(helper_l), _added(helper_r)]
    out = propagate_matches(matches)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert (helper_l.descriptor, helper_r.descriptor) not in paired


def test_call_site_anchor_resolves_what_declared_type_propagation_cannot():
    """Two small, mutually-identical decoy classes: content-only matching (and
    M1.3's declared-type propagation, which has no reference to either of
    them at all) can't tell them apart. The call site can, because each is
    instantiated by a distinctly-matched caller.
    """
    small = synthetic.standard_methods()
    decoy_a_l, decoy_a_r = _cls("La;", methods=small), _cls("La2;", methods=small)
    decoy_b_l, decoy_b_r = _cls("Lb;", methods=small), _cls("Lb2;", methods=small)

    caller1_l = _method("m1", instantiates=("La;",))
    caller1_r = _method("m1", instantiates=("La2;",))
    mm1 = MethodMatch(lhs=caller1_l, rhs=caller1_r, score=1.0, status="matched")
    caller1_match = _paired(_cls("Lc1;"), _cls("Lc1_2;"), method_matches=(mm1,))

    caller2_l = _method("m2", instantiates=("Lb;",))
    caller2_r = _method("m2", instantiates=("Lb2;",))
    mm2 = MethodMatch(lhs=caller2_l, rhs=caller2_r, score=1.0, status="matched")
    caller2_match = _paired(_cls("Lc2;"), _cls("Lc2_2;"), method_matches=(mm2,))

    matches = [
        caller1_match, caller2_match,
        _deleted(decoy_a_l), _deleted(decoy_b_l),
        _added(decoy_a_r), _added(decoy_b_r),
    ]
    out = propagate_matches(matches, threshold=0.8)
    paired = {(m.lhs.descriptor, m.rhs.descriptor) for m in out if m.is_paired}
    assert ("La;", "La2;") in paired
    assert ("Lb;", "Lb2;") in paired
