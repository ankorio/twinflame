"""Tier-1 containment scoring.

Containment(family ⊆ candidate) is instruction-weighted and asymmetric. These
tests pin the invariants the malware-triage flow relies on: self-containment is
1.0, a family embedded in a padded candidate still scores 1.0 (the reason
containment beats Jaccard), an unrelated candidate scores ~0, the score is
weighted by real logic, and library/boilerplate noise is excluded from the
family side so it can't inflate a cross-app score.
"""

from __future__ import annotations

from synthetic import ClassSpec, FieldSpec, MethodSpec, make_class

from twinflame.model import AccessFlag
from twinflame.score import (
    build_index,
    class_present,
    containment,
    family_classes,
    is_library,
)
from twinflame.signature import compute_signature

# Distinct framework call targets to hand out so each synthetic class gets a
# discriminative signature (tiny structurally-similar classes SimHash-collide —
# a real property, which is why the pipeline buckets with LSH then discriminates
# with accurate scoring; here we want unambiguous per-class signatures).
_CALLS = [f"Landroid/a{k}/S{k};->m{k}(I)V" for k in range(20)]

# Discriminating radius for tests that assert an exact present-count / index.
# The diverse `_big` classes below sit >= 10 Hamming apart pairwise, so 4 is a
# safe margin that still catches an exact self-match (distance 0).
_R = 4


def _big(descriptor, seed, source_file="X.java"):
    """A structurally-varied class: varying method/field counts and a distinct
    per-seed set of framework calls, so its signature is discriminative."""
    nmeth = 2 + seed % 4
    methods = [MethodSpec(name="<init>", access=AccessFlag.PUBLIC | AccessFlag.CONSTRUCTOR,
                          bytecode=bytes([0x70] * (2 + seed % 3) + [0x0E]), instr_count=6 + seed)]
    for mi in range(nmeth):
        start = (seed * 3 + mi) % len(_CALLS)
        methods.append(MethodSpec(
            name=f"work{mi}", descriptor="(II)I", return_type="I", arg_count=(seed + mi) % 4,
            bytecode=bytes([0x12 + (j % 6) for j in range(6 + seed + mi)] + [0x0F]),
            instr_count=10 + seed + mi,
            calls=tuple(_CALLS[start:start + 3])))
    fields = tuple(FieldSpec(name=f"f{j}", type_desc=("I", "J", "Ljava/lang/String;")[j % 3])
                   for j in range(seed % 5))
    return make_class(ClassSpec(descriptor=descriptor, source_file=source_file,
                                methods=tuple(methods), fields=fields))


def _family(n=6):
    return [_big(f"Lfam/pkg/C{i};", i) for i in range(n)]


# A disjoint framework-call vocabulary for building a genuinely *unrelated*
# app (different APIs → signatures that don't share the family's call tokens).
_OTHER_CALLS = [f"Ljavax/z{k}/T{k};->q{k}(J)V" for k in range(20)]


def _unrelated(descriptor, seed):
    nmeth = 2 + seed % 4
    methods = [MethodSpec(name="<init>", access=AccessFlag.PUBLIC | AccessFlag.CONSTRUCTOR,
                          bytecode=bytes([0x71] * (2 + seed % 3) + [0x0E]), instr_count=7 + seed)]
    for mi in range(nmeth):
        start = (seed * 3 + mi) % len(_OTHER_CALLS)
        methods.append(MethodSpec(
            name=f"run{mi}", descriptor="(J)J", return_type="J", arg_count=1,
            bytecode=bytes([0x60 + (j % 6) for j in range(6 + seed + mi)] + [0x10]),
            instr_count=11 + seed + mi,
            calls=tuple(_OTHER_CALLS[start:start + 3])))
    return make_class(ClassSpec(descriptor=descriptor, source_file="Y.java",
                                methods=tuple(methods)))


def test_self_containment_is_one():
    fam = _family()
    r = containment(fam, fam)
    assert r.score == 1.0
    assert r.present == r.total == len(fam)


def test_family_embedded_in_padded_candidate_is_one():
    """The core reason for containment over Jaccard: the family is fully present
    even when the candidate adds a large amount of unrelated (benign) code."""
    fam = _family(6)
    padding = [_big(f"Lbenign/pad/P{i};", 100 + i) for i in range(40)]
    r = containment(fam, fam + padding)
    assert r.score == 1.0
    # Jaccard would be ~6/46 here; containment ignores the padding.


def test_unrelated_candidate_scores_low():
    fam = _family(6)
    other = [_unrelated(f"Lother/app/U{i};", i) for i in range(6)]
    r = containment(fam, other, radius=_R)
    assert r.score < 0.5


def test_partial_containment_is_weighted():
    """Half the family present → score reflects the *instruction weight* of the
    present half, not a flat class-count fraction."""
    fam = _family(6)
    candidate = list(fam[:3])  # only the first three family classes are present
    r = containment(fam, candidate, radius=_R)
    assert r.present == 3
    assert 0.0 < r.score < 1.0
    # weight_present is the summed weight of exactly the present classes
    present_weight = sum(c.total_instructions + 1 for c in fam[:3])
    assert r.weight_present == present_weight


def test_library_classes_dropped_from_family():
    lib = make_class(ClassSpec(
        descriptor="Landroidx/core/app/NotificationCompat;",
        methods=(MethodSpec(name="build", bytecode=bytes([0x12, 0x0F]), instr_count=5),),
    ))
    app = _big("Lfam/pkg/Real;", 1)
    kept = family_classes([lib, app])
    assert app in kept and lib not in kept
    # …and keeping it (opt-out) includes it
    assert lib in family_classes([lib, app], drop_library=False)


def test_is_library_detects_prefix_and_maven_sourcefile():
    assert is_library(make_class(ClassSpec(descriptor="Lkotlin/collections/CollectionsKt;")))
    assert is_library(make_class(ClassSpec(descriptor="Lokhttp3/OkHttpClient;")))
    # Maven-coordinate SourceFile that R8 left intact
    assert is_library(make_class(ClassSpec(descriptor="Lx/y/Z;", source_file="com.foo:bar")))
    assert not is_library(make_class(ClassSpec(descriptor="Lfam/pkg/C;", source_file="C.java")))


def test_boilerplate_dropped_from_family():
    twin = make_class(ClassSpec(
        descriptor="Lfam/pkg/$$Cmp;", interfaces=("Ljava/util/Comparator;",),
        methods=(MethodSpec(name="compare", instr_count=3),)))
    real = _big("Lfam/pkg/Real;", 2)
    assert twin not in family_classes([twin, real])


def test_tiny_classes_dropped_from_family():
    empty = make_class(ClassSpec(
        descriptor="Lfam/pkg/Marker;",
        methods=(MethodSpec(name="x", bytecode=b"\x0e", instr_count=1),)))
    assert empty not in family_classes([empty])


def test_class_present_primitive():
    cand = _family(5)
    index = build_index(cand)
    hit = class_present(index, compute_signature(cand[2]), radius=_R)
    assert hit is not None and hit[0] == 2 and hit[1] == 0  # exact self-match, distance 0

    far = _big("Lnowhere/Z;", 999)
    assert class_present(index, compute_signature(far), radius=2) is None


def test_evidence_records_present_pairs_sorted():
    fam = _family(4)
    r = containment(fam, fam, radius=_R)
    assert len(r.evidence) == 4
    dists = [d for _, _, d in r.evidence]
    assert dists == sorted(dists)
    for f_desc, c_desc, d in r.evidence:
        assert f_desc == c_desc and d == 0  # self-diff: each maps to itself


def test_empty_family_scores_zero_not_crash():
    r = containment([], _family(3))
    assert r.score == 0.0 and r.total == 0


def test_min_shared_calls_gate_rejects_chance_signature_match():
    """A family class and a same-signature candidate that share NO framework
    calls are counted present at min_shared_calls=0 but rejected at 1 — the
    precision gate against density chance-matches on renamed builds."""
    fam_c = _big("Lfam/pkg/A;", 3)                 # calls come from _CALLS
    twin = _unrelated("Lcand/x/B;", 3)             # same seed → near signature, _OTHER_CALLS
    # force an exact signature collision so only the call gate can distinguish
    from dataclasses import replace
    twin = replace(twin, signature=compute_signature(fam_c))
    fam = [fam_c]

    base = containment(fam, [twin], radius=_R)
    assert base.present == 1                        # signature-only: counts it
    gated = containment(fam, [twin], radius=_R, min_shared_calls=1)
    assert gated.present == 0                       # no shared framework calls → rejected


def test_min_shared_calls_falls_back_for_call_poor_classes():
    """A family class with no framework calls can't be gated on shared calls, so
    it still matches on signature alone — the gate must not cost recall on
    call-poor real logic."""
    poor = make_class(ClassSpec(
        descriptor="Lfam/pkg/Poor;", source_file="Poor.java",
        methods=(MethodSpec(name="loop", bytecode=bytes([0x12, 0x13, 0x14, 0x0F]),
                            instr_count=8),)))
    r = containment([poor], [poor], radius=_R, min_shared_calls=2)
    assert r.present == 1  # matched on signature despite having 0 framework calls
