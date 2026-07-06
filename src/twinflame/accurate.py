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
    *,
    assignment: str = "greedy",
) -> list[MethodMatch]:
    """Assign lhs methods to rhs methods within an already-paired class.

    1-to-1 over the per-pair similarity; `assignment` selects greedy,
    Hungarian, or auto (M2.1) — see `select_assignment`. Unpaired lhs
    methods become "deleted", unpaired rhs methods "added".

    Defaults to "greedy", not "auto": `hungarian_assign` maximizes the
    *unweighted* sum of per-method scores, but the class score consumes
    these via `_method_rollup`'s *instruction-weighted* average — Hungarian
    can sacrifice a large method's correct pairing for marginal score gains
    on several small ones, which is optimal for its own (wrong, for this
    consumer) objective but not for the class-level result. Measured on the
    CalculatorM3 corpus: `--assignment auto`/`hungarian` at the method level
    *regressed* class-match recall (TP 62->57) despite class-level assignment
    being unaffected (pool too large to trigger auto's Hungarian branch
    there) — isolated by forcing method-level greedy and confirming it
    fully explained the regression. Not fixed with a weighted Hungarian
    variant here; scope kept to class-level assignment, which the plan's
    original acceptance criteria were about anyway.
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

    assigned = select_assignment(candidates, assignment)

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


def compare_classes(
    a: Class, b: Class, *, assignment: str = "greedy"
) -> tuple[float, dict[str, float], list[MethodMatch]]:
    """Method-level class comparison (M1.1).

    Produces per-method verdicts plus a class score that is the method roll-up
    blended with the structural feature similarity (same weights as the blob
    `class_similarity`, so identical classes still score exactly 1.0).
    `assignment` is forwarded to the intra-class method assignment — defaults
    to "greedy", not "auto"; see `match_methods`'s docstring for why Hungarian
    there is a measured net regression, not just an untested option.
    """
    method_matches = match_methods(a.methods, b.methods, assignment=assignment)
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

    Fast and usually fine, but *locally* greedy: on a pool of several
    look-alike candidates it can leave a strictly worse global pairing than
    the optimal one (the "identical-structure classes collide" limitation in
    the README) — see `hungarian_assign` for the exact fallback (M2.1).
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


# Cost placeholder for a (lhs, rhs) pair that was never scored (no LSH/full
# candidate relation between them) — strictly worse than any real cost
# (real cost = 1 - score, score in [0, 1], so real cost is always <= 1.0).
_NO_CANDIDATE_COST = 2.0
# Hungarian trigger heuristic (--assignment auto, M2.1): only worth the
# O(n^3) cost when the pool is small *and* there's real ambiguity to
# resolve (some rhs contested by 2+ lhs candidates) — otherwise greedy
# already finds the same optimal assignment for a fraction of the cost.
HUNGARIAN_MAX_POOL_SIZE = 100


def hungarian_assign(
    candidates: dict[int, list[tuple[int, float]]],
) -> dict[int, tuple[int, float]]:
    """Globally optimal 1-to-1 assignment (Kuhn-Munkres) over the same sparse
    top-k candidate structure `greedy_assign` accepts.

    Builds a square cost matrix over the union of lhs/rhs indices that
    appear anywhere in `candidates` (cost = 1 - score; unscored pairs get
    `_NO_CANDIDATE_COST`, padding rows/columns needed to square up an
    unequal lhs/rhs count get it too), solves it, then discards any result
    cell that lands on padding or an unscored pair — those lhs are left
    unassigned, same as `greedy_assign` would leave them.
    """
    if not candidates:
        return {}

    lhs_list = sorted(candidates.keys())
    rhs_set: set[int] = set()
    for ranked in candidates.values():
        for rhs_idx, _score in ranked:
            rhs_set.add(rhs_idx)
    if not rhs_set:
        return {}
    rhs_list = sorted(rhs_set)

    n = max(len(lhs_list), len(rhs_list))
    cost = [[_NO_CANDIDATE_COST] * n for _ in range(n)]
    lhs_index = {li: i for i, li in enumerate(lhs_list)}
    rhs_index = {rj: j for j, rj in enumerate(rhs_list)}
    for li, ranked in candidates.items():
        i = lhs_index[li]
        for rj, score in ranked:
            j = rhs_index[rj]
            c = 1.0 - score
            if c < cost[i][j]:
                cost[i][j] = c

    col_for_row = _solve_assignment(cost)

    result: dict[int, tuple[int, float]] = {}
    for i, li in enumerate(lhs_list):
        j = col_for_row[i]
        if j >= len(rhs_list):
            continue  # matched to a padding column, no real candidate there
        c = cost[i][j]
        if c >= _NO_CANDIDATE_COST:
            continue  # never actually scored against this rhs
        result[li] = (rhs_list[j], 1.0 - c)
    return result


def _solve_assignment(cost: list[list[float]]) -> list[int]:
    """Kuhn-Munkres (Hungarian algorithm) on a square cost matrix, O(n^3).

    Minimizes total cost. Returns `col_for_row`: row i is assigned to
    column `col_for_row[i]`. Standard shortest-augmenting-path-with-
    potentials formulation (1-indexed internally per the classic reference
    implementation; 0-indexed in and out).
    """
    n = len(cost)
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = row currently assigned to column j (0 = none)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = -1
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    col_for_row = [0] * n
    for j in range(1, n + 1):
        if p[j] != 0:
            col_for_row[p[j] - 1] = j - 1
    return col_for_row


def _has_contention(candidates: dict[int, list[tuple[int, float]]]) -> bool:
    """True if some rhs candidate is proposed for 2+ different lhs — the
    exact condition under which greedy's first-come-first-served resolution
    can diverge from the globally optimal assignment.
    """
    seen: set[int] = set()
    for ranked in candidates.values():
        for rhs_idx, _score in ranked:
            if rhs_idx in seen:
                return True
            seen.add(rhs_idx)
    return False


def select_assignment(
    candidates: dict[int, list[tuple[int, float]]], mode: str = "auto"
) -> dict[int, tuple[int, float]]:
    """Dispatch to `greedy_assign` or `hungarian_assign` per `mode`
    (`"greedy"`, `"hungarian"`, or `"auto"` — the `--assignment` CLI flag).

    `"auto"` only reaches for Hungarian when the pool is small enough that
    O(n^3) is cheap *and* there's genuine ambiguity to resolve; otherwise
    greedy already gives the same answer for a fraction of the cost.
    """
    if mode == "greedy":
        return greedy_assign(candidates)
    if mode == "hungarian":
        return hungarian_assign(candidates)

    n_lhs = len(candidates)
    n_rhs = len({rj for ranked in candidates.values() for rj, _score in ranked})
    if max(n_lhs, n_rhs) <= HUNGARIAN_MAX_POOL_SIZE and _has_contention(candidates):
        return hungarian_assign(candidates)
    return greedy_assign(candidates)
