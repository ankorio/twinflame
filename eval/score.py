"""Class-level precision/recall/F1 scorer for the M3.2 evaluation harness.

Grades twinflame's own class matches against real ground truth from a double
build (R8 off -> the `lhs`/donor input; R8 on -> the `rhs`/target input, with
its real `mapping.txt` as the oracle). This is the "renaming-only" slice: it
grades M1.1 (method-level matching feeds class scoring) + M1.2 (anchoring)
without touching inlining/outlining (that's M3.1's job, gated on this harness
per the dev plan).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from twinflame.model import Class, Match


def _fqcn(cls: Class) -> str:
    return f"{cls.package}.{cls.name}" if cls.package else cls.name


@dataclass(frozen=True, slots=True)
class ScoreResult:
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def score_class_matches(matches: Iterable[Match], ground_truth: dict[str, str]) -> ScoreResult:
    """Score twinflame's paired class matches against a real mapping.txt oracle.

    `ground_truth` is `{original_fqcn (lhs/donor side): obfuscated_fqcn (rhs/
    target side)}`, as parsed by `eval.mapping.load_class_mapping`. Only
    ground-truth entries are gradable — an lhs class outside its scope (e.g.
    not present in the donor build the mapping was generated from) is neither
    a hit nor a miss.
    """
    true_positives = 0
    false_positives = 0
    hit_originals: set[str] = set()

    for m in matches:
        if not m.is_paired:
            continue
        lhs_fqcn = _fqcn(m.lhs)
        rhs_fqcn = _fqcn(m.rhs)
        expected = ground_truth.get(lhs_fqcn)
        if expected is None:
            continue
        if expected == rhs_fqcn:
            true_positives += 1
            hit_originals.add(lhs_fqcn)
        else:
            false_positives += 1

    false_negatives = sum(1 for orig in ground_truth if orig not in hit_originals)
    return ScoreResult(true_positives, false_positives, false_negatives)
