"""Class-level precision/recall/F1 scorer (eval/score.py, M3.2)."""

from __future__ import annotations

import synthetic

from twinflame.model import Match
from eval.score import score_class_matches


def _cls(descriptor):
    return synthetic.make_class(synthetic.ClassSpec(descriptor=descriptor))


def _paired(lhs_desc, rhs_desc, distance=1.0):
    return Match(lhs=_cls(lhs_desc), rhs=_cls(rhs_desc), distance=distance)


def test_true_positive_matches_ground_truth():
    matches = [_paired("Lcom/acme/Login;", "La/a;")]
    ground_truth = {"com.acme.Login": "a.a"}
    result = score_class_matches(matches, ground_truth)
    assert (result.true_positives, result.false_positives, result.false_negatives) == (1, 0, 0)
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.f1 == 1.0


def test_false_positive_when_matched_to_wrong_class():
    matches = [_paired("Lcom/acme/Login;", "La/b;")]  # ground truth says a.a
    ground_truth = {"com.acme.Login": "a.a"}
    result = score_class_matches(matches, ground_truth)
    assert (result.true_positives, result.false_positives, result.false_negatives) == (0, 1, 1)


def test_false_negative_when_ground_truth_class_unmatched():
    result = score_class_matches([], {"com.acme.Login": "a.a"})
    assert (result.true_positives, result.false_positives, result.false_negatives) == (0, 0, 1)


def test_lhs_outside_ground_truth_scope_is_ignored():
    matches = [_paired("Lcom/acme/NotInCorpus;", "La/z;")]
    ground_truth = {"com.acme.Login": "a.a"}
    result = score_class_matches(matches, ground_truth)
    assert (result.true_positives, result.false_positives, result.false_negatives) == (0, 0, 1)


def test_unpaired_matches_are_ignored():
    added = Match(lhs=None, rhs=_cls("La/z;"), distance=0.0)
    deleted = Match(lhs=_cls("Lcom/acme/Gone;"), rhs=None, distance=0.0)
    result = score_class_matches([added, deleted], {"com.acme.Login": "a.a"})
    assert (result.true_positives, result.false_positives, result.false_negatives) == (0, 0, 1)


def test_precision_recall_f1_zero_division_safe():
    result = score_class_matches([], {})
    assert result.precision == 0.0
    assert result.recall == 0.0
    assert result.f1 == 0.0
