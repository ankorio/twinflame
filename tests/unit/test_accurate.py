from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from twinflame.accurate import (
    BYTECODE_WEIGHT,
    FEATURE_WEIGHT,
    HUNGARIAN_MAX_POOL_SIZE,
    abstract_sequence,
    class_similarity,
    greedy_assign,
    hungarian_assign,
    select_assignment,
)
from twinflame.model import AccessFlag

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


# --- hungarian_assign / select_assignment (M2.1) --------------------------


def test_hungarian_assign_empty():
    assert hungarian_assign({}) == {}


def test_hungarian_assign_no_candidates_for_any_lhs():
    assert hungarian_assign({0: [], 1: []}) == {}


def test_hungarian_assign_matches_greedy_when_unambiguous():
    candidates = {0: [(0, 0.9)], 1: [(1, 0.8)]}
    assert hungarian_assign(candidates) == greedy_assign(candidates) == {0: (0, 0.9), 1: (1, 0.8)}


def test_hungarian_recovers_the_correct_global_pairing_where_greedy_fails():
    """The exact scenario the README's "identical-structure classes collide"
    limitation describes: lhs0 and lhs1 both want rhs0 (lhs1 more strongly),
    but lhs1 has *no other option* while lhs0 can fall back to rhs1. Greedy's
    first-come-first-served claiming lets lhs0 grab rhs0 first (its score is
    highest in the full sorted list), leaving lhs1 completely unmatched even
    though a strictly better *and* fully-matched assignment exists.
    """
    candidates = {
        0: [(0, 1.0), (1, 0.9)],
        1: [(0, 0.95)],
        2: [(1, 0.8), (2, 0.1)],
    }
    greedy = greedy_assign(candidates)
    hungarian = hungarian_assign(candidates)

    # Document the failure mode this fixes: greedy leaves lhs1 unmatched.
    assert 1 not in greedy
    assert greedy[0] == (0, 1.0)

    # Hungarian finds the strictly better, fully-matched assignment.
    assert set(hungarian) == {0, 1, 2}
    assert hungarian[1][0] == 0 and hungarian[1][1] == pytest.approx(0.95)
    assert hungarian[0][0] == 1 and hungarian[0][1] == pytest.approx(0.9)
    assert hungarian[2][0] == 2 and hungarian[2][1] == pytest.approx(0.1)

    def total_score(assignment):
        return sum(score for _rhs, score in assignment.values())

    assert total_score(hungarian) > total_score(greedy)


def _brute_force_optimal_total(candidates: dict[int, list[tuple[int, float]]]) -> float:
    """Exhaustive reference: try every 1-to-1 assignment, return the best
    achievable total score. Only tractable for the small cases these tests
    use — this is the ground truth `hungarian_assign` is checked against.
    """
    lhs_list = list(candidates)
    rhs_options = {li: dict(ranked) for li, ranked in candidates.items()}
    best = 0.0

    def search(i: int, used: set[int], total: float) -> float:
        nonlocal best
        if i == len(lhs_list):
            best = max(best, total)
            return best
        li = lhs_list[i]
        # Option: leave li unassigned.
        search(i + 1, used, total)
        for rhs_idx, score in rhs_options[li].items():
            if rhs_idx in used:
                continue
            search(i + 1, used | {rhs_idx}, total + score)
        return best

    return search(0, set(), 0.0)


def test_hungarian_assign_matches_brute_force_optimum_on_random_pools():
    rng = random.Random(1234)
    for _ in range(30):
        n_lhs = rng.randint(1, 5)
        n_rhs = rng.randint(1, 5)
        candidates: dict[int, list[tuple[int, float]]] = {}
        for li in range(n_lhs):
            ranked = [
                (rj, round(rng.random(), 3))
                for rj in range(n_rhs)
                if rng.random() < 0.7  # sparse, like real top-k LSH candidates
            ]
            candidates[li] = ranked

        hungarian = hungarian_assign(candidates)
        hungarian_total = sum(score for _rhs, score in hungarian.values())
        optimal_total = _brute_force_optimal_total(candidates)
        assert hungarian_total == pytest.approx(optimal_total, abs=1e-9)

        # And it's a genuine 1-to-1 mapping.
        rhs_used = [rhs for rhs, _score in hungarian.values()]
        assert len(rhs_used) == len(set(rhs_used))


def test_select_assignment_greedy_mode_matches_greedy_assign():
    candidates = {0: [(0, 1.0), (1, 0.9)], 1: [(0, 0.95)], 2: [(1, 0.8), (2, 0.1)]}
    assert select_assignment(candidates, "greedy") == greedy_assign(candidates)


def test_select_assignment_hungarian_mode_matches_hungarian_assign():
    candidates = {0: [(0, 1.0), (1, 0.9)], 1: [(0, 0.95)], 2: [(1, 0.8), (2, 0.1)]}
    assert select_assignment(candidates, "hungarian") == hungarian_assign(candidates)


def test_select_assignment_auto_uses_hungarian_when_pool_is_small_and_contested():
    candidates = {0: [(0, 1.0), (1, 0.9)], 1: [(0, 0.95)], 2: [(1, 0.8), (2, 0.1)]}
    assert select_assignment(candidates, "auto") == hungarian_assign(candidates)
    assert select_assignment(candidates, "auto") != greedy_assign(candidates)


def test_select_assignment_auto_falls_back_to_greedy_when_no_contention():
    # No rhs is proposed by more than one lhs -- greedy already finds the
    # same (only possible) optimal answer, so auto shouldn't bother with
    # the O(n^3) Hungarian pass.
    candidates = {0: [(0, 0.9)], 1: [(1, 0.8)], 2: [(2, 0.7)]}
    assert select_assignment(candidates, "auto") == greedy_assign(candidates)


def test_select_assignment_auto_falls_back_to_greedy_on_large_pools():
    # Contested, but over HUNGARIAN_MAX_POOL_SIZE -- auto should stay greedy
    # rather than pay the O(n^3) cost.
    n = HUNGARIAN_MAX_POOL_SIZE + 5
    candidates = {i: [(0, 0.5 + i * 1e-6), (i + 1, 0.4)] for i in range(n)}
    assert select_assignment(candidates, "auto") == greedy_assign(candidates)


def test_greedy_assign_empty():
    assert greedy_assign({}) == {}
