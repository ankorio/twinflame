"""M1.2 — anchoring through the full diff pipeline.

Builds a pool of structurally-identical classes (SimHash alone cannot tell
them apart) where each lhs class shares a unique string with exactly one rhs
class. With anchoring on, every class pairs to its true partner; the per-class
method detail rides along on the resulting Match objects.
"""

from __future__ import annotations

import synthetic

from apkdiff import api


def _twin_classes(n: int):
    """n identical-structure classes per side, paired only by a unique string.

    rhs is emitted in reversed order and with source files stripped (as R8
    would), so position/name give the matcher no help.
    """
    methods = synthetic.standard_methods()
    fields = synthetic.standard_fields()

    def make(desc, src, token):
        return synthetic.make_class(
            synthetic.ClassSpec(
                descriptor=desc,
                source_file=src,
                methods=methods,
                fields=fields,
                strings=(f"anchor-token-{token}-qwerty",),
            )
        )

    lhs = [make(f"Lcom/vendor/Clear{i};", f"Clear{i}.java", i) for i in range(n)]
    rhs = [make(f"Lobf/{chr(97 + j)};", "SourceFile", j) for j in reversed(range(n))]
    return lhs, rhs


def test_anchoring_pairs_ambiguous_classes_by_string():
    lhs, rhs = _twin_classes(4)
    matches = api.diff(
        lhs, rhs, threshold=0.8, optimizations={"anchoring": True, "cluster": False}
    )
    paired = [m for m in matches if m.is_paired]
    assert len(paired) == 4
    # Each clear-text lhs must pair with the rhs carrying the same token.
    for m in paired:
        li = m.lhs.name.replace("Clear", "")
        assert f"anchor-token-{li}-qwerty" in m.rhs.strings
        assert m.breakdown.get("anchored") == 1.0


def test_disabling_anchoring_loses_the_string_signal():
    lhs, rhs = _twin_classes(4)
    matches = api.diff(
        lhs, rhs, threshold=0.8, optimizations={"anchoring": False, "cluster": False}
    )
    paired = [m for m in matches if m.is_paired]
    # Structure is identical across all classes, so without anchoring the
    # by-string correspondence is not recovered: at least one pairing is wrong
    # (or unpaired). This documents *why* anchoring earns its keep.
    correct = sum(
        1
        for m in paired
        if f"anchor-token-{m.lhs.name.replace('Clear', '')}-qwerty" in m.rhs.strings
    )
    assert correct < 4


def test_anchored_pair_carries_method_detail():
    lhs, rhs = _twin_classes(2)
    matches = api.diff(
        lhs, rhs, threshold=0.8, optimizations={"anchoring": True, "cluster": False}
    )
    paired = [m for m in matches if m.is_paired]
    assert paired
    for m in paired:
        # standard_methods() are identical on both sides → all matched.
        assert m.method_matches
        assert all(mm.status == "matched" for mm in m.method_matches)
