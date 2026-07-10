"""M1.2 — anchoring stage (string-IDF + framework-call seed matches)."""

from __future__ import annotations

import synthetic

from twinflame.anchor import framework_calls, seed_anchors


def _cls(desc, *, strings=(), calls=()):
    methods = (
        synthetic.MethodSpec(
            name="m",
            bytecode=bytes([0x12, 0x6E, 0x0E]),
            instr_count=3,
            calls=calls,
        ),
    )
    return synthetic.make_class(
        synthetic.ClassSpec(descriptor=desc, methods=methods, strings=strings)
    )


def test_no_anchors_without_evidence():
    lhs = [_cls("La;"), _cls("Lb;")]
    rhs = [_cls("Lx;"), _cls("Ly;")]
    assert seed_anchors(lhs, rhs) == []


def test_unique_string_pairs_structurally_identical_classes():
    # Same structure everywhere → SimHash can't disambiguate; only the unique
    # string tells us which lhs maps to which rhs. rhs is shuffled.
    lhs = [_cls(f"Lobf/a{i};", strings=(f"unique-tag-{i}-zzzz",)) for i in range(3)]
    rhs = [_cls(f"Lobf/z{j};", strings=(f"unique-tag-{j}-zzzz",)) for j in (2, 0, 1)]
    anchors = seed_anchors(lhs, rhs)
    mapping = {li: ri for li, ri, _ in anchors}
    # token i lives at lhs[i]; in rhs the shuffle (2,0,1) puts token i at the
    # position whose value is i → rhs index .index(i).
    expected = {i: [2, 0, 1].index(i) for i in range(3)}
    assert mapping == expected
    assert all(conf >= 0.5 for _, _, conf in anchors)


def test_common_string_is_not_a_strong_anchor():
    # A string shared by every class carries no discriminating signal.
    lhs = [_cls(f"La{i};", strings=("the-same-boilerplate",)) for i in range(3)]
    rhs = [_cls(f"Lz{i};", strings=("the-same-boilerplate",)) for i in range(3)]
    anchors = seed_anchors(lhs, rhs, min_confidence=0.5)
    # Low IDF, split across many candidates → nothing clears the bar.
    assert anchors == []


def test_anchors_resolve_one_to_one():
    lhs = [_cls("La;", strings=("alpha-marker-xyz",)), _cls("Lb;", strings=("beta-marker-xyz",))]
    rhs = [_cls("Lz;", strings=("beta-marker-xyz",)), _cls("Ly;", strings=("alpha-marker-xyz",))]
    anchors = seed_anchors(lhs, rhs)
    lhs_used = [li for li, _, _ in anchors]
    rhs_used = [ri for _, ri, _ in anchors]
    assert len(lhs_used) == len(set(lhs_used))
    assert len(rhs_used) == len(set(rhs_used))


def test_framework_calls_keeps_only_framework_targets():
    c = _cls(
        "La;",
        calls=(
            "Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V",
            "Lcom/acme/Internal;->helper()V",  # app code, dropped
            "Ljava/lang/String;->length()I",
        ),
    )
    fc = framework_calls(c)
    assert "Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V" in fc
    assert "Ljava/lang/String;->length()I" in fc
    assert all("Lcom/acme" not in ref for ref in fc)


def _cls_super(desc, superclass, *, interfaces=(), strings=()):
    methods = (synthetic.MethodSpec(name="m", bytecode=bytes([0x12, 0x0E]),
                                    instr_count=2),)
    return synthetic.make_class(synthetic.ClassSpec(
        descriptor=desc, methods=methods, strings=strings,
        superclass=superclass, interfaces=interfaces))


def test_distinctive_framework_supertype_anchors_stringless_classes():
    # Fillers give the corpus size that makes a rarely-used framework supertype
    # distinctive by IDF. The pair below shares a framework superclass held by
    # exactly one class per side and has NO strings — unreachable by the string
    # anchor, surfaced by the hierarchy signal.
    fillers_l = [_cls_super(f"La{i};", "Ljava/lang/Object;") for i in range(20)]
    fillers_r = [_cls_super(f"Lx{i};", "Ljava/lang/Object;") for i in range(20)]
    lhs = fillers_l + [_cls_super("Lp;", "Landroidx/room/RoomDatabase;")]
    rhs = fillers_r + [_cls_super("Lq;", "Landroidx/room/RoomDatabase;")]
    anchors = seed_anchors(lhs, rhs)
    assert (20, 20) in {(li, ri) for li, ri, _ in anchors}


def test_common_framework_supertype_does_not_anchor():
    # A framework supertype shared by many classes each side is not distinctive
    # (low IDF) and must not mint anchors.
    lhs = [_cls_super(f"La{i};", "Landroidx/app/Activity;") for i in range(15)]
    rhs = [_cls_super(f"Lx{i};", "Landroidx/app/Activity;") for i in range(15)]
    assert seed_anchors(lhs, rhs) == []
