"""Minor-version diff gate.

Mutate one method's bytecode in one class; the diff must surface exactly
one match below 1.0 and identify the mutated class.
"""

from __future__ import annotations

import synthetic

from twinflame import api


def test_minor_diff_identifies_mutated_class():
    lhs = synthetic.vendor_app(n_classes=6)
    # Mutate Class3.getValue() — inject extra opcodes
    mutated_idx = 3
    rhs = list(lhs)
    rhs[mutated_idx] = synthetic.mutate_method_bytecode(
        rhs[mutated_idx],
        method_name="op0",
        new_bytecode=bytes([0x12, 0x12, 0x90, 0x91, 0x6E, 0x6E, 0x0F]),
    )

    matches = api.diff(lhs, rhs, threshold=0.6)
    paired = sorted([m for m in matches if m.is_paired], key=lambda m: m.distance)
    # The mutated class should be the lowest-scoring paired match.
    assert len(paired) == 6
    # Exactly one match should be below 1.0
    below = [m for m in paired if m.distance < 1.0]
    assert len(below) == 1, f"expected 1 mutated match, got {len(below)}"
    mutated_match = below[0]
    assert f"Class{mutated_idx}" in mutated_match.lhs.info
    assert f"Class{mutated_idx}" in mutated_match.rhs.info


def test_added_class_appears_as_added_only():
    lhs = synthetic.vendor_app(n_classes=4)
    rhs = synthetic.vendor_app(n_classes=5)  # rhs has one extra Class4
    matches = api.diff(lhs, rhs, threshold=0.8)
    added = [m for m in matches if m.is_added]
    deleted = [m for m in matches if m.is_deleted]
    paired = [m for m in matches if m.is_paired]
    assert len(paired) == 4
    assert len(added) == 1
    assert len(deleted) == 0
    assert "Class4" in added[0].rhs.info


def test_deleted_class_appears_as_deleted_only():
    lhs = synthetic.vendor_app(n_classes=5)
    rhs = synthetic.vendor_app(n_classes=4)
    matches = api.diff(lhs, rhs, threshold=0.8)
    deleted = [m for m in matches if m.is_deleted]
    added = [m for m in matches if m.is_added]
    paired = [m for m in matches if m.is_paired]
    assert len(paired) == 4
    assert len(deleted) == 1
    assert len(added) == 0
    assert "Class4" in deleted[0].lhs.info
