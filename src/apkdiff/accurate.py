from __future__ import annotations

from collections import Counter

from ._hot import levenshtein_bytes
from .model import AccessFlag, Class
from .opcodes import categorize


BYTECODE_WEIGHT = 0.6
FEATURE_WEIGHT = 0.4


def abstract_sequence(c: Class) -> bytes:
    methods = sorted(c.methods, key=lambda m: m.order_key)
    parts = [categorize(m.bytecode) for m in methods if m.bytecode]
    return b"".join(parts)


def _ratio(a: int, b: int) -> float:
    if a == b == 0:
        return 1.0
    return 1.0 - abs(a - b) / max(a, b, 1)


def _jaccard_multiset(a: Counter, b: Counter) -> float:
    if not a and not b:
        return 1.0
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 1.0


def _access_flags(c: Class) -> Counter:
    return Counter(flag.name for flag in AccessFlag if c.access & flag)


def _field_types(c: Class) -> Counter:
    return Counter(f.type_desc for f in c.fields)


def _method_protos(c: Class) -> Counter:
    return Counter((m.return_type, m.arg_count) for m in c.methods)


def class_similarity(a: Class, b: Class) -> tuple[float, dict[str, float]]:
    seq_a = abstract_sequence(a)
    seq_b = abstract_sequence(b)
    if not seq_a and not seq_b:
        bytecode_sim = 1.0
    else:
        dist = levenshtein_bytes(seq_a, seq_b)
        bytecode_sim = 1.0 - dist / max(len(seq_a), len(seq_b), 1)

    feat = {
        "nmethods": _ratio(len(a.methods), len(b.methods)),
        "nfields": _ratio(len(a.fields), len(b.fields)),
        "access": _jaccard_multiset(_access_flags(a), _access_flags(b)),
        "field_types": _jaccard_multiset(_field_types(a), _field_types(b)),
        "method_protos": _jaccard_multiset(_method_protos(a), _method_protos(b)),
    }
    feature_avg = sum(feat.values()) / len(feat)
    overall = BYTECODE_WEIGHT * bytecode_sim + FEATURE_WEIGHT * feature_avg
    breakdown = {**feat, "bytecode": bytecode_sim, "overall": overall}
    return overall, breakdown


def greedy_assign(
    candidates: dict[int, list[tuple[int, float]]],
) -> dict[int, tuple[int, float]]:
    """Resolve top-k candidate lists into a deterministic 1-to-1 mapping.

    Walks all (lhs, rhs, score) triples in descending score order. The first
    lhs to claim a given rhs wins; later lhs claimants advance to their next
    unclaimed neighbor. Returns lhs_idx -> (rhs_idx, score).
    """
    triples: list[tuple[int, int, float]] = []
    for lhs_idx, ranked in candidates.items():
        for rhs_idx, score in ranked:
            triples.append((lhs_idx, rhs_idx, score))
    # Descending score, then ascending lhs_idx, then ascending rhs_idx for
    # deterministic tiebreaking.
    triples.sort(key=lambda t: (-t[2], t[0], t[1]))

    claimed_rhs: set[int] = set()
    claimed_lhs: set[int] = set()
    result: dict[int, tuple[int, float]] = {}
    for lhs_idx, rhs_idx, score in triples:
        if lhs_idx in claimed_lhs or rhs_idx in claimed_rhs:
            continue
        result[lhs_idx] = (rhs_idx, score)
        claimed_lhs.add(lhs_idx)
        claimed_rhs.add(rhs_idx)
    return result
