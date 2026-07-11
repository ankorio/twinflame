"""Tier-1 containment scoring — app-to-app kinship as a scalar.

    ⚠️ WORK IN PROGRESS / EXPERIMENTAL — not production-ready.
    The primitive is correct (self-containment = 1.0, asymmetric) and useful for
    self/near-self kinship, but it does NOT yet yield a trustworthy family-vs-
    benign threshold on renamed builds: it depends on the not-yet-built library
    signature dictionary (dev-plan M3.3) to strip shared-runtime noise, and its
    knobs (`radius`, `min_shared_calls`, a screen threshold) are uncalibrated —
    they need a real positive/negative family corpus. Treat every number below
    the 1.0 self-anchor as indicative, not final. See the Status section of
    the batch-scoring design.

The matcher answers "which class here corresponds to which class there"; this
answers "how much of family F's code is present in candidate C" as a single
number, cheaply, off prepared records. It is the load-bearing primitive for the
malware-triage flow in the batch-scoring design: a prefilter (e.g. YARA)
flags a candidate as *possibly* family F, and twinflame confirms/scores it
against a known family seed without diffing the whole corpus.

**Primary score = containment, family ⊆ candidate** (locked decision, user
2026-07-07): the instruction-weighted fraction of the family's code structurally
present in the candidate. Chosen over Jaccard because repackaged malware embeds
the family plus benign padding — Jaccard's union term dilutes that, containment
does not.

**Tier-1 = signatures only.** A family class is *present* iff the candidate has a
class whose 128-bit signature is within Hamming radius `R` of it, found via the
existing `LSHIndex` — no per-method Levenshtein. That's the accurate diff's job
(Tier-2 evidence, on demand). The signatures already fold in the rename-/
optimization-invariant tokens (framework super/iface/call), so this rests on the
obfuscation-robust substrate.

**Noise exclusion is mandatory on the family side.** Shared AndroidX/Kotlin/Gson
code would otherwise inflate containment across *unrelated* apps. Boilerplate
(`is_boilerplate`) and known-library classes are dropped from the family class
set before scoring. The library filter is two-layered: the package-prefix
denylist (`provenance.LIBRARY_PREFIXES`, catches un-renamed code) plus — when a
catalogue pack is available (`--libsigs` / `libdict.py`) — the M3.3
signature-dictionary exclusions via `library_descriptors`, which reach the
renamed shared-runtime classes no prefix heuristic can see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .anchor import FRAMEWORK_PREFIXES
from .boilerplate import is_boilerplate
from .model import Class, Signature
from .provenance import LIBRARY_PREFIXES
from .signature import LSHIndex, compute_signature

# Default Hamming radius for "same class" on the 128-bit signature. Calibrated
# to the structural-match regime the diff already trusts: the top-k LSH
# neighbors that clear the 0.8 accurate threshold sit within a few bits per
# 32-bit part. 12/128 is deliberately a touch generous (containment biases
# toward *finding* family code; a false "present" is caught by Tier-2 evidence,
# a false "absent" silently lowers the score). Tune against a positive/negative
# corpus — see the Eval section of the batch-scoring design.
DEFAULT_MATCH_RADIUS = 12

# A family class carrying almost no structure is a near-universal shape (empty
# marker, tiny interface) that would match something in any candidate. Excluded
# from the family set for the same reason the diff filters them.
MIN_FAMILY_INSTRUCTIONS = 2

# Optional precision gate on "present" (default off). A signature within `radius`
# is *necessary* but, on renamed builds with the library stopgap, not sufficient:
# a large candidate has a class within a few bits of almost any small low-entropy
# signature by density alone, so unrelated apps that share the Kotlin/AndroidX
# runtime score deceptively high. Requiring the matched candidate class to share
# >= N framework calls with the family class adds the rename-invariant behavioral
# evidence R8 can't fake. Measured on one positive (private app 1.8.2⊆1.9.1) and
# one negative (contacts⊆telegram) pair, N=2 widened the family/unrelated gap
# from +0.11 to +0.33 — promising but calibrated on a single pair, so it stays
# OFF by default until a real positive/negative family corpus sets the threshold
# (the batch-scoring design, Eval). The real noise fix is M3.3 (a library
# signature dictionary) removing shared-runtime classes at the source.
MIN_SHARED_CALLS_DEFAULT = 0


def _framework_calls(c: Class) -> frozenset[str]:
    return frozenset(
        ref for m in c.methods for ref in m.calls if ref.startswith(FRAMEWORK_PREFIXES)
    )


def is_library(c: Class) -> bool:
    """Known-library stopgap: descriptor under a library prefix, or a Maven-
    coordinate `SourceFile` (`group:artifact`) that R8 left intact. Replaced by
    the signature dictionary (M3.3) once it exists."""
    if c.descriptor.startswith(LIBRARY_PREFIXES):
        return True
    if c.source_file and ":" in c.source_file:
        return True
    return False


def family_classes(
    classes: Iterable[Class],
    *,
    drop_library: bool = True,
    min_instructions: int = MIN_FAMILY_INSTRUCTIONS,
    library_descriptors: Optional[frozenset[str]] = None,
) -> list[Class]:
    """The family's *scoreable* class set: real logic only. Drops boilerplate
    twins, tiny/near-empty shapes, and (by default) known-library code — the
    noise that would otherwise inflate containment against unrelated apps.
    `library_descriptors` (from `libdict.label_classes` over the family) adds
    the dictionary's evidence-based exclusions on top of the prefix stopgap —
    this is the M3.3 fix: renamed shared-runtime classes leave the family set
    even though no prefix heuristic can see them."""
    out: list[Class] = []
    for c in classes:
        if is_boilerplate(c):
            continue
        if c.total_instructions < min_instructions:
            continue
        if drop_library and is_library(c):
            continue
        if library_descriptors and c.descriptor in library_descriptors:
            continue
        out.append(c)
    return out


def _weight(c: Class) -> int:
    # Instruction-weighted so real logic dominates boilerplate; +1 so a small
    # but kept class still contributes (its body cleared MIN_FAMILY_INSTRUCTIONS).
    return c.total_instructions + 1


def build_index(classes: Iterable[Class]) -> LSHIndex:
    """LSH index over a candidate's class signatures, keyed by class position
    so a hit can be traced back to the specific candidate class."""
    index = LSHIndex()
    for i, c in enumerate(classes):
        index.add(compute_signature(c), i)
    return index


def class_present(
    index: LSHIndex, sig: Signature, radius: int = DEFAULT_MATCH_RADIUS
) -> Optional[tuple[int, int]]:
    """Nearest candidate class within `radius` Hamming bits of `sig`, as
    `(candidate_index, distance)`, or `None`. The single Tier-1 primitive."""
    hits = index.query(sig, k=1)
    if hits and hits[0][1] <= radius:
        return hits[0]
    return None


@dataclass(frozen=True, slots=True)
class ContainmentResult:
    """`score` = instruction-weighted fraction of the family present in the
    candidate. `present`/`total` are the family class counts behind it;
    `weight_present`/`weight_total` the weighted sums the score is computed
    from. `evidence` pairs each present family class descriptor with the
    candidate class it matched and the Hamming distance (for Tier-2 follow-up)."""

    score: float
    present: int
    total: int
    weight_present: int
    weight_total: int
    evidence: tuple[tuple[str, str, int], ...]


def containment(
    family: list[Class],
    candidate: list[Class],
    *,
    radius: int = DEFAULT_MATCH_RADIUS,
    drop_library: bool = True,
    min_shared_calls: int = MIN_SHARED_CALLS_DEFAULT,
    library_descriptors: Optional[frozenset[str]] = None,
) -> ContainmentResult:
    """Score `containment(family ⊆ candidate)` off two class lists (typically
    `record.app.classes` from prepared records). The family set is noise-
    filtered; the candidate is indexed whole (we want to discover family code
    *wherever* it landed, including under repackaged names).

    `min_shared_calls` (default off) gates a signature match on the two classes
    also sharing >= N framework calls — but only for family classes that *have*
    at least that many framework calls, so call-poor real logic still matches on
    signature alone (no recall loss). See `MIN_SHARED_CALLS_DEFAULT`."""
    fam = family_classes(family, drop_library=drop_library,
                         library_descriptors=library_descriptors)
    index = build_index(candidate)

    weight_total = weight_present = 0
    present = 0
    evidence: list[tuple[str, str, int]] = []
    for c in fam:
        w = _weight(c)
        weight_total += w
        hit = class_present(index, compute_signature(c), radius)
        if hit is None:
            continue
        cand_i, dist = hit
        if min_shared_calls:
            fam_calls = _framework_calls(c)
            # Only enforce the gate when the family class carries enough
            # framework-call evidence to test; otherwise fall back to signature.
            if len(fam_calls) >= min_shared_calls:
                shared = fam_calls & _framework_calls(candidate[cand_i])
                if len(shared) < min_shared_calls:
                    continue
        weight_present += w
        present += 1
        evidence.append((c.descriptor, candidate[cand_i].descriptor, dist))

    score = weight_present / weight_total if weight_total else 0.0
    evidence.sort(key=lambda e: e[2])  # closest matches first
    return ContainmentResult(
        score=score, present=present, total=len(fam),
        weight_present=weight_present, weight_total=weight_total,
        evidence=tuple(evidence),
    )
