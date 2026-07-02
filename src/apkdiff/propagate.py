"""Match propagation (plan D2 / report §5.2, M1.3) — type-graph cascade.

`csl-ugent/apkdiff`'s `matchOnMatch` cascade is a genuine idea the four-stage
core (cluster -> anchor -> SimHash/LSH -> accurate) doesn't do: once two
classes are confirmed matched, their superclass, interfaces, and field/
method-referenced types can be inferred with high confidence, for free — no
LSH cost. Anchoring (M1.2) seeds *before* structural matching from content
(strings, calls); this seeds *after* a match is confirmed, from structure
(the type graph). The two are complementary.

Runs as a post-pass over the full match list, after every pool has resolved
— a propagated type reference can point outside the pool that produced the
originating match (e.g. a shared interface in a different package), so this
needs a global view rather than a per-pool one.
"""

from __future__ import annotations

from .accurate import compare_classes, greedy_assign
from .model import Class, Match
from .signature import LSHIndex, compute_signature

# Referenced-type extraction is capped per class so a pathological
# interface-heavy or field-heavy class can't blow up a round's candidate set.
MAX_REFS_PER_CLASS = 32
# Hard cap on fixpoint rounds — a newly propagated match can itself propagate
# further, so this bounds worst-case cost on a large, densely-connected app.
MAX_ROUNDS = 25
# Top-k SimHash/LSH neighbors scored per lhs candidate (same mechanism the
# main pool pass uses) — a round's candidate set on a real app can run into
# the hundreds/thousands of distinct unmatched referenced types (e.g. many
# confirmed matches sharing one common base class), so exhaustive O(L*R)
# `compare_classes` — full method-level Levenshtein — doesn't scale. LSH
# narrows scoring to O(L*k) without changing which pairs *can* be found.
TOP_K_NEIGHBORS = 3
# Defensive cap on a round's candidate-set size (pathological input only —
# LSH already bounds the real cost, this just stops index-build from growing
# unboundedly on adversarial data).
MAX_CANDIDATES_PER_SIDE = 50_000


def _extract_object_types(descriptor: str) -> list[str]:
    """Pull every "Lfoo/Bar;" token out of a field/method type descriptor.

    Handles array prefixes ("[Lfoo/Bar;" -> "Lfoo/Bar;") and multi-arg method
    descriptors ("(ILfoo/Bar;)Lbaz/Qux;") by scanning for 'L' ... ';' spans.
    """
    out: list[str] = []
    i, n = 0, len(descriptor)
    while i < n:
        c = descriptor[i]
        if c == "L":
            end = descriptor.find(";", i)
            if end == -1:
                break
            out.append(descriptor[i : end + 1])
            i = end + 1
        else:
            i += 1
    return out


def referenced_descriptors(c: Class) -> list[str]:
    """Object-type descriptors `c`'s type graph points at.

    Superclass, interfaces, field types, and method proto types (params +
    return). Primitives are dropped; arrays-of-objects are unwrapped to their
    element descriptor. Deduped, order-preserved, capped.
    """
    out: list[str] = []
    if c.superclass:
        out.append(c.superclass)
    out.extend(c.interfaces)
    for f in c.fields:
        out.extend(_extract_object_types(f.type_desc))
    for m in c.methods:
        out.extend(_extract_object_types(m.descriptor))

    seen: set[str] = set()
    result: list[str] = []
    for d in out:
        if d in seen:
            continue
        seen.add(d)
        result.append(d)
        if len(result) >= MAX_REFS_PER_CLASS:
            break
    return result


def propagate_matches(matches: list[Match], *, threshold: float = 0.8) -> list[Match]:
    """Cascade new matches from confirmed pairs via the type graph.

    Every input match is preserved, except a "deleted"/"added" entry whose
    class gets claimed by propagation is replaced by the new paired match.
    Never touches or downgrades an existing paired match.
    """
    lhs_by_desc: dict[str, Class] = {}
    rhs_by_desc: dict[str, Class] = {}
    paired_lhs: set[str] = set()
    paired_rhs: set[str] = set()
    queue: list[Match] = []

    for m in matches:
        if m.lhs is not None:
            lhs_by_desc[m.lhs.descriptor] = m.lhs
        if m.rhs is not None:
            rhs_by_desc[m.rhs.descriptor] = m.rhs
        if m.is_paired:
            paired_lhs.add(m.lhs.descriptor)
            paired_rhs.add(m.rhs.descriptor)
            queue.append(m)

    new_matches: list[Match] = []
    rounds = 0
    while queue and rounds < MAX_ROUNDS:
        rounds += 1
        lhs_candidates: dict[str, Class] = {}
        rhs_candidates: dict[str, Class] = {}
        for m in queue:
            for d in referenced_descriptors(m.lhs):
                if d not in paired_lhs and d in lhs_by_desc:
                    lhs_candidates[d] = lhs_by_desc[d]
            for d in referenced_descriptors(m.rhs):
                if d not in paired_rhs and d in rhs_by_desc:
                    rhs_candidates[d] = rhs_by_desc[d]

        if not lhs_candidates or not rhs_candidates:
            break
        if (
            len(lhs_candidates) > MAX_CANDIDATES_PER_SIDE
            or len(rhs_candidates) > MAX_CANDIDATES_PER_SIDE
        ):
            break

        lhs_list = list(lhs_candidates.values())
        rhs_list = list(rhs_candidates.values())

        index = LSHIndex()
        for j, rc in enumerate(rhs_list):
            index.add(compute_signature(rc), payload=j)

        candidates: dict[int, list[tuple[int, float]]] = {}
        for i, lc in enumerate(lhs_list):
            neighbors = index.query(compute_signature(lc), k=TOP_K_NEIGHBORS)
            ranked = [(j, compare_classes(lc, rhs_list[j])[0]) for j, _h in neighbors]
            ranked.sort(key=lambda t: -t[1])
            candidates[i] = ranked

        assigned = greedy_assign(candidates)

        next_queue: list[Match] = []
        for i, (j, _rank) in assigned.items():
            lc, rc = lhs_list[i], rhs_list[j]
            dist, breakdown, mms = compare_classes(lc, rc)
            if dist < threshold:
                continue
            breakdown["propagated"] = 1.0
            pm = Match(lc, rc, dist, breakdown, tuple(mms))
            new_matches.append(pm)
            paired_lhs.add(lc.descriptor)
            paired_rhs.add(rc.descriptor)
            next_queue.append(pm)

        queue = next_queue

    if not new_matches:
        return matches

    claimed_lhs = {m.lhs.descriptor for m in new_matches}
    claimed_rhs = {m.rhs.descriptor for m in new_matches}
    out: list[Match] = []
    for m in matches:
        if m.is_deleted and m.lhs.descriptor in claimed_lhs:
            continue
        if m.is_added and m.rhs.descriptor in claimed_rhs:
            continue
        out.append(m)
    out.extend(new_matches)
    return out
