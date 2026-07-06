"""M2.1 — --assignment flag through the full diff pipeline.

The core Hungarian-vs-greedy correctness question is covered exhaustively at
the unit level (tests/unit/test_accurate.py, including brute-force
verification against random pools) — this just confirms the flag actually
reaches the pool pass and doesn't change behavior when there's nothing
ambiguous to resolve.
"""

from __future__ import annotations

import synthetic

from twinflame import api


def _twins(n: int):
    """n identical-structure classes per side — SimHash alone can't tell
    them apart, so every lhs is a genuine candidate for every rhs (mirrors
    test_anchor_diff.py's ambiguity setup, without an anchor to resolve it).
    """
    methods = synthetic.standard_methods()
    fields = synthetic.standard_fields()

    def make(desc):
        return synthetic.make_class(
            synthetic.ClassSpec(descriptor=desc, methods=methods, fields=fields)
        )

    lhs = [make(f"Lcom/vendor/Clear{i};") for i in range(n)]
    rhs = [make(f"Lobf/{chr(97 + j)};") for j in range(n)]
    return lhs, rhs


def test_assignment_flag_accepted_for_all_modes():
    lhs, rhs = _twins(3)
    for mode in ("greedy", "hungarian", "auto"):
        matches = api.diff(
            lhs, rhs, threshold=0.8, optimizations={"cluster": False, "assignment": mode}
        )
        paired = [m for m in matches if m.is_paired]
        # Identical structure on both sides -> every class finds *a* full-score
        # match (which specific one is ambiguous by construction; that's fine).
        assert len(paired) == 3
        assert all(m.distance == 1.0 for m in paired)


def test_hungarian_mode_matches_greedy_when_unambiguous():
    """No shared candidates across lhs -> only one possible optimal
    assignment; Hungarian and greedy must agree.
    """
    methods = synthetic.standard_methods()

    def make(desc, extra_fields):
        return synthetic.make_class(
            synthetic.ClassSpec(descriptor=desc, methods=methods, fields=extra_fields)
        )

    lhs = [
        make("Lcom/vendor/A;", (synthetic.FieldSpec(name="a", type_desc="I"),)),
        make("Lcom/vendor/B;", (synthetic.FieldSpec(name="b", type_desc="J"),)),
    ]
    rhs = [
        make("Lobf/a;", (synthetic.FieldSpec(name="a", type_desc="I"),)),
        make("Lobf/b;", (synthetic.FieldSpec(name="b", type_desc="J"),)),
    ]

    greedy_matches = api.diff(
        lhs, rhs, threshold=0.8, optimizations={"cluster": False, "assignment": "greedy"}
    )
    hungarian_matches = api.diff(
        lhs, rhs, threshold=0.8, optimizations={"cluster": False, "assignment": "hungarian"}
    )
    greedy_pairs = {(m.lhs.descriptor, m.rhs.descriptor) for m in greedy_matches if m.is_paired}
    hungarian_pairs = {
        (m.lhs.descriptor, m.rhs.descriptor) for m in hungarian_matches if m.is_paired
    }
    assert greedy_pairs == hungarian_pairs == {
        ("Lcom/vendor/A;", "Lobf/a;"),
        ("Lcom/vendor/B;", "Lobf/b;"),
    }
