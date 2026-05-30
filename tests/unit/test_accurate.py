from __future__ import annotations

import sys
from pathlib import Path

from apkdiff.accurate import (
    BYTECODE_WEIGHT,
    FEATURE_WEIGHT,
    abstract_sequence,
    class_similarity,
    greedy_assign,
)
from apkdiff.model import AccessFlag

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic  # noqa: E402


def test_abstract_sequence_sorts_methods_by_order_key():
    spec_a = synthetic.ClassSpec(
        descriptor="LA;",
        methods=(
            synthetic.MethodSpec(name="m1", bytecode=bytes([0x6E, 0x0E]), instr_count=2),
            synthetic.MethodSpec(name="m2", bytecode=bytes([0x12, 0x0F]), instr_count=2),
        ),
    )
    # Same methods, declared in reverse order — should yield the same sequence
    spec_b = synthetic.ClassSpec(
        descriptor="LA;",
        methods=tuple(reversed(spec_a.methods)),
    )
    a = synthetic.make_class(spec_a)
    b = synthetic.make_class(spec_b)
    assert abstract_sequence(a) == abstract_sequence(b)


def test_abstract_sequence_empty_class_yields_empty():
    c = synthetic.make_class(synthetic.ClassSpec(descriptor="LA;", methods=()))
    assert abstract_sequence(c) == b""


def test_class_similarity_self_is_one():
    c = synthetic.make_class(
        synthetic.ClassSpec(
            descriptor="LA;",
            methods=synthetic.standard_methods(),
            fields=synthetic.standard_fields(),
        )
    )
    score, breakdown = class_similarity(c, c)
    assert score == 1.0
    assert breakdown["bytecode"] == 1.0
    assert breakdown["overall"] == 1.0


def test_class_similarity_differs_when_bytecode_mutated():
    base = synthetic.make_class(
        synthetic.ClassSpec(
            descriptor="LA;",
            methods=synthetic.standard_methods(),
            fields=synthetic.standard_fields(),
        )
    )
    mutated = synthetic.mutate_method_bytecode(
        base, "getValue", bytes([0x12, 0x12, 0x6E, 0x6E, 0x0E])
    )
    score, breakdown = class_similarity(base, mutated)
    assert 0.0 <= score < 1.0
    assert breakdown["bytecode"] < 1.0
    # Non-bytecode features should still match perfectly
    assert breakdown["nmethods"] == 1.0
    assert breakdown["nfields"] == 1.0


def test_class_similarity_obeys_weight_constants():
    base = synthetic.make_class(
        synthetic.ClassSpec(descriptor="LA;", methods=synthetic.standard_methods())
    )
    score, b = class_similarity(base, base)
    # When everything matches, overall == 1.0 = 0.6*1 + 0.4*1
    assert BYTECODE_WEIGHT + FEATURE_WEIGHT == 1.0
    assert score == 1.0


def test_class_similarity_unrelated_classes_score_low():
    a = synthetic.make_class(
        synthetic.ClassSpec(
            descriptor="LA;",
            methods=(synthetic.MethodSpec(name="m", bytecode=bytes([0x6E, 0x0E]), instr_count=2),),
        )
    )
    b = synthetic.make_class(
        synthetic.ClassSpec(
            descriptor="LB;",
            methods=tuple(
                synthetic.MethodSpec(
                    name=f"m{i}",
                    bytecode=bytes([0x12, 0x90, 0x91, 0x92, 0x6E, 0x0F]),
                    instr_count=6,
                )
                for i in range(5)
            ),
            fields=synthetic.standard_fields(),
        )
    )
    score, _ = class_similarity(a, b)
    assert score < 0.6


def test_greedy_assign_resolves_contested_match():
    # Two lhs both want rhs=0; higher score wins, loser falls back.
    candidates = {
        0: [(0, 0.9), (1, 0.5)],   # lhs 0 prefers rhs 0
        1: [(0, 0.95), (1, 0.4)],  # lhs 1 prefers rhs 0 even more strongly
    }
    out = greedy_assign(candidates)
    assert out[1] == (0, 0.95)
    assert out[0] == (1, 0.5)


def test_greedy_assign_handles_no_fallback():
    # lhs 0 only proposes rhs 0; if claimed, lhs 0 gets nothing.
    candidates = {
        0: [(0, 0.7)],
        1: [(0, 0.9)],
    }
    out = greedy_assign(candidates)
    assert out[1] == (0, 0.9)
    assert 0 not in out


def test_greedy_assign_is_deterministic_on_ties():
    candidates = {
        0: [(0, 0.5), (1, 0.5)],
        1: [(0, 0.5), (1, 0.5)],
    }
    a = greedy_assign(candidates)
    b = greedy_assign(candidates)
    assert a == b


def test_greedy_assign_empty():
    assert greedy_assign({}) == {}
