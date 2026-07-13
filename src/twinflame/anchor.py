"""Anchoring stage (plan Section B) — R8-invariant seed matches.

R8 renames identifiers but cannot rewrite the *content* of a string constant or
the *target* of a framework/library call. Two classes that share a rare string
(a unique log tag, URL, or error message) or call the same distinctive set of
`android/*`, `androidx/*`, `java/*`, `kotlin/*` APIs are almost certainly the
same class even after renaming. We turn those invariants into high-confidence
"anchor" pairs that the matcher locks in *before* structural scoring runs.

Implemented here: B1 (framework-call anchors), B2 (string-IDF anchors), B7
(seed generator). Deferred per the dev plan: B3 numeric/crypto constants, B4 JNI
signatures, B5 resource IDs, B6 reflection strings — the loader already captures
the call/string hooks they would build on.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from .model import Class

# Library/framework descriptor prefixes that survive R8 renaming verbatim.
FRAMEWORK_PREFIXES = (
    "Landroid/",
    "Landroidx/",
    "Ljava/",
    "Ljavax/",
    "Lkotlin/",
    "Lkotlinx/",
)

# Strings shorter than this, or purely numeric, carry too little signal.
MIN_STRING_LEN = 4
# Weight of the framework-call agreement relative to string evidence. Kept
# small: framework calls disambiguate string-derived candidates, they don't
# mint anchors on their own.
FRAMEWORK_WEIGHT = 0.25
# Weight of a shared framework supertype/interface as anchor evidence, relative
# to a string. Full weight: a framework supertype survives R8 verbatim (cf.
# LibPecker/apkdiff) and reaches string-less classes the string anchor can't.
# Commonness is already handled by IDF — a supertype shared by many classes has
# low idf and cannot anchor — so a *discount* here only suppresses the useful
# case (a framework type shared by exactly one class on each side, the strongest
# hierarchy signal). Measured across the OSS corpus at 1.0: net +recall, no
# precision loss; a 0.5 discount was fully inert (couldn't clear the threshold).
HIERARCHY_WEIGHT = 1.0
# The near-universal default superclass — no signal, excluded from hierarchy tokens.
_OBJECT_DESC = "Ljava/lang/Object;"
# Minimum combined confidence to accept a pair as an anchor.
DEFAULT_MIN_CONFIDENCE = 0.5


def useful_strings(c: Class) -> set[str]:
    return {s for s in c.strings if len(s) >= MIN_STRING_LEN and not s.isdigit()}


def hierarchy_tokens(c: Class) -> set[str]:
    """Rename-invariant framework supertype/interface tokens (Object excluded).
    App supertypes are renamed per build and so carry no cross-build signal."""
    toks: set[str] = set()
    sc = c.superclass
    if sc and sc != _OBJECT_DESC and sc.startswith(FRAMEWORK_PREFIXES):
        toks.add("S:" + sc)
    for iface in c.interfaces:
        if iface.startswith(FRAMEWORK_PREFIXES):
            toks.add("I:" + iface)
    return toks


def framework_calls(c: Class) -> Counter:
    """Multiset of framework/library call targets made by a class."""
    counts: Counter = Counter()
    for m in c.methods:
        for ref in m.calls:
            if ref.startswith(FRAMEWORK_PREFIXES):
                counts[ref] += 1
    return counts


def _jaccard(a: Counter, b: Counter) -> float:
    if not a and not b:
        return 0.0
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 0.0


def seed_anchors(
    lhs: list[Class],
    rhs: list[Class],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[tuple[int, int, float]]:
    """Return high-confidence (lhs_idx, rhs_idx, confidence) anchor pairs.

    String evidence dominates and is IDF-weighted: a string shared by exactly
    one class on each side contributes confidence 1.0 to that pair; common
    strings are down-weighted by their document frequency and split across the
    candidates they touch. Framework-call agreement adds a small bonus. Pairs
    are then resolved 1-to-1 greedily by confidence.
    """
    if not lhs or not rhs:
        return []

    # Token space = useful strings (weight 1.0) ∪ framework hierarchy tokens
    # (weight HIERARCHY_WEIGHT). Hierarchy tokens are namespaced with a leading
    # NUL so they can never collide with a real app string.
    def tokens(c: Class) -> dict[str, float]:
        toks: dict[str, float] = {s: 1.0 for s in useful_strings(c)}
        for h in hierarchy_tokens(c):
            toks["\x00" + h] = HIERARCHY_WEIGHT
        return toks

    lhs_tokens = [tokens(c) for c in lhs]
    rhs_tokens = [tokens(c) for c in rhs]

    n = len(lhs) + len(rhs)
    df: Counter = Counter()
    for ts in lhs_tokens:
        df.update(ts.keys())
    for ts in rhs_tokens:
        df.update(ts.keys())
    lhs_df: Counter = Counter()
    for ts in lhs_tokens:
        lhs_df.update(ts.keys())

    rhs_by_tok: dict[str, list[int]] = defaultdict(list)
    for j, ts in enumerate(rhs_tokens):
        for s in ts:
            rhs_by_tok[s].append(j)

    # idf of a token seen exactly once == log(n+1); use it to normalize so a
    # single unique shared string yields evidence 1.0.
    norm = math.log(n + 1) or 1.0

    evidence: dict[tuple[int, int], float] = defaultdict(float)
    for li, ts in enumerate(lhs_tokens):
        for s, weight in ts.items():
            ris = rhs_by_tok.get(s)
            if not ris:
                continue
            tok_idf = math.log((n + 1) / df[s])
            split = lhs_df[s] * len(ris)  # ambiguity penalty
            w = weight * (tok_idf / norm) / split
            for ri in ris:
                evidence[(li, ri)] += w

    if not evidence:
        return []

    # Framework-call agreement nudges string-derived candidates. Cache the
    # per-class call multisets so each is computed at most once.
    fw_lhs: dict[int, Counter] = {}
    fw_rhs: dict[int, Counter] = {}
    for (li, ri) in list(evidence):
        a = fw_lhs.setdefault(li, framework_calls(lhs[li]))
        b = fw_rhs.setdefault(ri, framework_calls(rhs[ri]))
        if a or b:
            evidence[(li, ri)] += FRAMEWORK_WEIGHT * _jaccard(a, b)

    ranked = sorted(
        ((conf, li, ri) for (li, ri), conf in evidence.items()),
        key=lambda t: (-t[0], t[1], t[2]),
    )

    used_l: set[int] = set()
    used_r: set[int] = set()
    anchors: list[tuple[int, int, float]] = []
    for raw, li, ri in ranked:
        conf = min(1.0, raw)
        if conf < min_confidence:
            break
        if li in used_l or ri in used_r:
            continue
        used_l.add(li)
        used_r.add(ri)
        anchors.append((li, ri, conf))
    return anchors
