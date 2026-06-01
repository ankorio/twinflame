from __future__ import annotations

from collections import Counter

from ._hot import levenshtein_bytes
from .model import AccessFlag, Class, Method, MethodMatch
from .opcodes import categorize


BYTECODE_WEIGHT = 0.6
FEATURE_WEIGHT = 0.4

# Per-method scoring weights (M1.1). Sum to 1.0 so an identical method → 1.0.
METHOD_BYTECODE_WEIGHT = 0.7
METHOD_PROTO_WEIGHT = 0.2
METHOD_XREF_WEIGHT = 0.1
# A candidate method pair scoring below this is not paired; the lhs method is
# reported "deleted" and the rhs "added" instead of a low-confidence "modified".
MIN_METHOD_PAIR_SCORE = 0.4


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


def method_similarity(a: Method, b: Method) -> float:
    """Score a single method pair in [0,1].

    Abstract-opcode Levenshtein dominates; prototype (return type + arg count)
    and the incoming-call count (xref) refine it. Identical methods score 1.0.
    """
    cat_a = categorize(a.bytecode)
    cat_b = categorize(b.bytecode)
    if not cat_a and not cat_b:
        bytecode_sim = 1.0
    else:
        dist = levenshtein_bytes(cat_a, cat_b)
        bytecode_sim = 1.0 - dist / max(len(cat_a), len(cat_b), 1)

    ret_eq = 1.0 if a.return_type == b.return_type else 0.0
    proto_sim = 0.5 * ret_eq + 0.5 * _ratio(a.arg_count, b.arg_count)
    xref_sim = _ratio(a.xref_count, b.xref_count)

    score = (
        METHOD_BYTECODE_WEIGHT * bytecode_sim
        + METHOD_PROTO_WEIGHT * proto_sim
        + METHOD_XREF_WEIGHT * xref_sim
    )
    # The weights sum to 1.0 but float arithmetic lands at 0.999…9 when every
    # component is perfect; snap so an identical method scores exactly 1.0 and
    # classifies as "matched" rather than "modified".
    return 1.0 if score >= 1.0 - 1e-9 else score


def match_methods(
    a_methods: tuple[Method, ...],
    b_methods: tuple[Method, ...],
) -> list[MethodMatch]:
    """Assign lhs methods to rhs methods within an already-paired class.

    Greedy 1-to-1 over the per-pair similarity (mirrors `greedy_assign`).
    Unpaired lhs methods become "deleted", unpaired rhs methods "added".
    """
    candidates: dict[int, list[tuple[int, float]]] = {}
    for i, ma in enumerate(a_methods):
        ranked = [
            (j, s)
            for j, mb in enumerate(b_methods)
            if (s := method_similarity(ma, mb)) >= MIN_METHOD_PAIR_SCORE
        ]
        ranked.sort(key=lambda t: -t[1])
        candidates[i] = ranked

    assigned = greedy_assign(candidates)

    result: list[MethodMatch] = []
    claimed_b: set[int] = set()
    for i, ma in enumerate(a_methods):
        pair = assigned.get(i)
        if pair is None:
            result.append(MethodMatch(lhs=ma, rhs=None, score=0.0, status="deleted"))
            continue
        j, s = pair
        claimed_b.add(j)
        status = "matched" if s >= 1.0 else "modified"
        result.append(MethodMatch(lhs=ma, rhs=b_methods[j], score=s, status=status))
    for j, mb in enumerate(b_methods):
        if j not in claimed_b:
            result.append(MethodMatch(lhs=None, rhs=mb, score=0.0, status="added"))
    return result


def _method_rollup(method_matches: list[MethodMatch]) -> float:
    """Instruction-weighted average of per-method scores.

    Added/deleted methods contribute their instruction weight at score 0, so a
    class that gained or lost a method is penalized proportionally. An empty
    class (no methods either side) rolls up to 1.0 (nothing differs).
    """
    total_w = 0.0
    acc = 0.0
    for mm in method_matches:
        instr = 0
        if mm.lhs is not None:
            instr = max(instr, mm.lhs.instr_count)
        if mm.rhs is not None:
            instr = max(instr, mm.rhs.instr_count)
        w = instr + 1.0  # +1 so trivial (0-instr) methods still register
        total_w += w
        acc += w * mm.score
    if total_w == 0.0:
        return 1.0
    return acc / total_w


def compare_classes(a: Class, b: Class) -> tuple[float, dict[str, float], list[MethodMatch]]:
    """Method-level class comparison (M1.1).

    Produces per-method verdicts plus a class score that is the method roll-up
    blended with the structural feature similarity (same weights as the blob
    `class_similarity`, so identical classes still score exactly 1.0).
    """
    method_matches = match_methods(a.methods, b.methods)
    method_agg = _method_rollup(method_matches)

    feat = {
        "nmethods": _ratio(len(a.methods), len(b.methods)),
        "nfields": _ratio(len(a.fields), len(b.fields)),
        "access": _jaccard_multiset(_access_flags(a), _access_flags(b)),
        "field_types": _jaccard_multiset(_field_types(a), _field_types(b)),
        "method_protos": _jaccard_multiset(_method_protos(a), _method_protos(b)),
    }
    feature_avg = sum(feat.values()) / len(feat)
    overall = BYTECODE_WEIGHT * method_agg + FEATURE_WEIGHT * feature_avg

    counts = Counter(mm.status for mm in method_matches)
    breakdown = {
        **feat,
        "bytecode": method_agg,
        "overall": overall,
        "methods_matched": float(counts.get("matched", 0)),
        "methods_modified": float(counts.get("modified", 0)),
        "methods_added": float(counts.get("added", 0)),
        "methods_deleted": float(counts.get("deleted", 0)),
    }
    return overall, breakdown, method_matches


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
