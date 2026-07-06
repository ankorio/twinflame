"""Match propagation (plan D2 / report §5.2, M1.3) — type-graph cascade.

`csl-ugent/apkdiff`'s `matchOnMatch` cascade is a genuine idea the four-stage
core (cluster -> anchor -> SimHash/LSH -> accurate) doesn't do: once two
classes are confirmed matched, their superclass, interfaces, and field/
method-referenced types can be inferred with high confidence, for free — no
LSH cost. Anchoring (M1.2) seeds *before* structural matching from content
(strings, calls); this seeds *after* a match is confirmed, from structure
(the type graph). The two are complementary.

Also includes M1.4's call-site anchor: once two *methods* are matched (M1.1),
the classes they instantiate via `new-instance` can be correlated by
position. This targets a gap declared-type propagation can't reach — small
classes (Kotlin lambda/coroutine continuation bodies) with real logic but too
little structure to fingerprint by content alone. Found via the M3.2
CalculatorM3 diagnosis: relaxing the loader's synthetic/small-class filters
to let these classes into ordinary structural matching made results *worse*
(more false positives, same or worse recall) — content-only matching can't
tell many small, near-identical classes apart. Their real identity is
*where they're instantiated*, not their own tiny body.

Runs as a post-pass over the full match list, after every pool has resolved
— a propagated type reference (or an instantiation site) can point outside
the pool that produced the originating match (e.g. a shared interface, or a
helper class instantiated from a different package), so this needs a global
view rather than a per-pool one.
"""

from __future__ import annotations

from .accurate import compare_classes, select_assignment
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
# Sanity floor for call-site-anchored pairs (M1.4) — deliberately much lower
# than the main structural `threshold`. The positional correlation *is* the
# confidence signal here (R8 can't decouple a `new-instance` from its call
# site), not the candidate's own structural similarity — a Kotlin `Companion`
# object or thin wrapper class is nearly empty, so content-based scoring is
# close to uninformative for it. On the real CalculatorM3 corpus, verified
# correct call-site-anchored pairs scored as low as 0.24 against the default
# 0.8 `threshold`, which silently rejected all of them. This floor exists
# only to reject a genuinely nonsensical pairing (e.g. alignment drifted
# because R8 added/removed an instantiation earlier in the same method), not
# to gate confidence the way the open-candidate-search threshold does.
CALL_SITE_MIN_CONFIDENCE = 0.15


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


def instantiation_pairs(m: Match) -> list[tuple[str, str]]:
    """Positionally-correlated (lhs, rhs) descriptor pairs from `new-instance`
    sites within `m`'s already-matched methods (M1.4).

    If matched caller method A(lhs) instantiates [X, Y, Z] in that order and
    its counterpart A'(rhs) instantiates [X', Y', Z'], zip them by position —
    R8 can rename the instantiated class and reshuffle unrelated code, but it
    can't disconnect a `new-instance` from its call site. Lengths can differ
    (e.g. one side had a dead instantiation optimized away); `zip` truncates
    to the shorter list rather than guessing at an alignment past that point.
    """
    pairs: list[tuple[str, str]] = []
    for mm in m.method_matches:
        if mm.lhs is None or mm.rhs is None:
            continue
        pairs.extend(zip(mm.lhs.instantiates, mm.rhs.instantiates))
    return pairs


def propagate_matches(
    matches: list[Match], *, threshold: float = 0.8, assignment: str = "auto"
) -> list[Match]:
    """Cascade new matches from confirmed pairs via the type graph (M1.3) and
    matched methods' instantiation sites (M1.4).

    Every input match is preserved, except a "deleted"/"added" entry whose
    class gets claimed is replaced by the new paired match. Never touches or
    downgrades an existing paired match — including a *wrong* one from an
    earlier stage: if a class was already (mis)claimed upstream, propagation/
    anchoring can't fix that here.

    `assignment` (M2.1: "greedy"/"hungarian"/"auto") is forwarded only to the
    declared-type cross-product's own class-level assignment step — *not* to
    `compare_classes`'s method-level scoring, which always stays greedy
    regardless (see `accurate.py::match_methods`'s docstring for why
    Hungarian there is a measured regression, not an oversight). A wrong
    *class-level* claim from an earlier pool pass is still final either way
    — using Hungarian upstream (in `api.py::_diff_pool`) is what actually
    prevents that class of bug, not this post-pass.
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
        next_queue: list[Match] = []

        # --- M1.4: direct positional pairs from matched methods' new-instance
        # sites. No candidate search needed — position already tells us which
        # lhs goes with which rhs, so just verify and accept/reject.
        seen_pairs: set[tuple[str, str]] = set()
        for m in queue:
            for ld, rd in instantiation_pairs(m):
                if (ld, rd) in seen_pairs:
                    continue
                seen_pairs.add((ld, rd))
                if ld in paired_lhs or rd in paired_rhs:
                    continue
                lc, rc = lhs_by_desc.get(ld), rhs_by_desc.get(rd)
                if lc is None or rc is None:
                    continue
                dist, breakdown, mms = compare_classes(lc, rc)
                if dist < CALL_SITE_MIN_CONFIDENCE:
                    continue
                breakdown["call_site_anchored"] = 1.0
                pm = Match(lc, rc, dist, breakdown, tuple(mms))
                new_matches.append(pm)
                paired_lhs.add(ld)
                paired_rhs.add(rd)
                next_queue.append(pm)

        # --- M1.3: declared-type-reference cross product (existing).
        lhs_candidates: dict[str, Class] = {}
        rhs_candidates: dict[str, Class] = {}
        for m in queue:
            for d in referenced_descriptors(m.lhs):
                if d not in paired_lhs and d in lhs_by_desc:
                    lhs_candidates[d] = lhs_by_desc[d]
            for d in referenced_descriptors(m.rhs):
                if d not in paired_rhs and d in rhs_by_desc:
                    rhs_candidates[d] = rhs_by_desc[d]

        if (
            lhs_candidates
            and rhs_candidates
            and len(lhs_candidates) <= MAX_CANDIDATES_PER_SIDE
            and len(rhs_candidates) <= MAX_CANDIDATES_PER_SIDE
        ):
            lhs_list = list(lhs_candidates.values())
            rhs_list = list(rhs_candidates.values())

            index = LSHIndex()
            for j, rc in enumerate(rhs_list):
                index.add(compute_signature(rc), payload=j)

            candidates: dict[int, list[tuple[int, float]]] = {}
            for i, lc in enumerate(lhs_list):
                neighbors = index.query(compute_signature(lc), k=TOP_K_NEIGHBORS)
                ranked = [
                    (j, compare_classes(lc, rhs_list[j])[0])
                    for j, _h in neighbors
                ]
                ranked.sort(key=lambda t: -t[1])
                candidates[i] = ranked

            assigned = select_assignment(candidates, assignment)
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

        if not next_queue:
            break
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
