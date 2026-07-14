"""Malware family signatures — shared structure across samples, persisted.

`build_family` distills N samples (APK / .dex / dex dir / prepared `.tfr`) of
one family into the class structure they share: per-sample noise filtering
(`score.family_classes` + the library dictionary), then single-linkage radius
clustering of the 128-bit signatures across samples, keeping clusters present
in >= k of N samples. The result is written as a `.tflp` pack (the libsigs
container, `meta.kind = "family"`), so `twinflame_libsigs`' detector matches
it against unknown samples unchanged; `match_family` aggregates those
per-class hits into a containment score over the family's entries.

Radii are asymmetric by design. Building answers "is this the *same class*
across builds of the family" — the regime the library detector calibrated
(radius 4, min-instr 20), and single-linkage chains amplify a generous radius,
so the build stays conservative. Matching answers containment, where a false
"absent" silently lowers the score, so queries default to the scorer's
`DEFAULT_MATCH_RADIUS` (12).

`twinflame_libsigs` is required for building and matching (the pack writer,
MIH store, and detector live there); unlike `libdict`'s optional labeling,
these entry points raise instead of degrading — a family verdict must never
silently come from a broken pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

from .model import Class, Signature
from .signature import compute_signature

# Build-side clustering radius: the corpus-validated "same class, different
# build" operating point (see libdict / libsigs findings), NOT the match
# radius — see the module docstring.
DEFAULT_BUILD_RADIUS = 4
# Classes below this floor are near-universal shapes at any radius (the
# libsigs 0.56 -> 0.83 precision finding); applied when building AND probing.
DEFAULT_MIN_INSTRUCTIONS = 20
# Family entries keep at most this many invariant strings (tier-3 anchors).
MAX_ENTRY_STRINGS = 32
# Medoid computation is O(c^2) per cluster; cap the member sample for blobs.
_MEDOID_CAP = 256

PACK_KIND = "family"

_LIBSIGS_HINT = (
    "family packs require the twinflame_libsigs package "
    "(pip install twinflame_libsigs)"
)


@dataclass(frozen=True, slots=True)
class FamilySample:
    """One ingested sample: identity plus its filtered, deduped class set."""
    sample_id: int
    label: str
    digest: str
    classes: Tuple[Class, ...]


@dataclass(frozen=True, slots=True)
class FamilyEntry:
    """One shared-structure cluster, reduced to its medoid representative."""
    signature: int                 # medoid combined 128-bit signature
    fqcn: str                      # medoid class name (dotted)
    strings: Tuple[str, ...]       # invariant strings (intersection, capped)
    instructions: int              # medoid total_instructions (match weight)
    coverage: int                  # distinct samples containing the cluster
    sample_ids: Tuple[int, ...]
    max_dist: int                  # cluster spread: max Hamming to the medoid


@dataclass(frozen=True, slots=True)
class FamilySignature:
    name: str
    n_samples: int
    k: int
    radius_build: int
    min_instructions: int
    samples: Tuple[Tuple[str, str], ...]   # (label, digest)
    entries: Tuple[FamilyEntry, ...]


@dataclass(frozen=True, slots=True)
class FamilyMatchResult:
    family: str
    pack: str
    score: float                   # coverage- and instruction-weighted containment
    present: int
    total: int
    weight_present: int
    weight_total: int
    by_tier: Dict[int, int]        # tier (1 exact / 2 radius / 3 string) -> entries
    evidence: Tuple[Tuple[str, str, int, int], ...]  # (fqcn, cand desc, tier, dist)


# --- ingestion ----------------------------------------------------------------

def load_family_sample(path: Path, *, normalize: bool = False) -> tuple[str, str, list[Class]]:
    """`(label, digest, classes)` for one input. A `.tfr` record loads directly
    (stamp-checked — a stale record raises `ValueError` naming the fix); other
    inputs go through `prepare` (APK / .dex / dex dir)."""
    from .prepare import is_record_file, load_record, prepare

    p = Path(path)
    rec = load_record(p) if is_record_file(p) else prepare(p, redex_normalize=normalize)
    return p.name, rec.digest, list(rec.classes)


def filter_sample_classes(
    classes: Iterable[Class],
    *,
    detector=None,
    min_instructions: int = DEFAULT_MIN_INSTRUCTIONS,
) -> tuple[list[Class], int]:
    """One sample's family-signature candidates: `score.family_classes` noise
    filtering, dictionary-recognised library classes dropped when a `detector`
    is given, then exact-signature dedup *within* the sample (coverage must
    count distinct samples, not distinct copies). Returns
    `(kept, n_library_excluded)`."""
    from .score import family_classes

    library_descriptors = None
    if detector is not None:
        from .libdict import label_classes
        library_descriptors = frozenset(label_classes(classes, detector))
    kept = family_classes(
        classes,
        min_instructions=min_instructions,
        library_descriptors=library_descriptors,
    )
    out: list[Class] = []
    seen: set[int] = set()
    for c in kept:
        sig = (c.signature or compute_signature(c)).combined
        if sig in seen:
            continue
        seen.add(sig)
        out.append(c)
    return out, len(library_descriptors or ())


# --- build ---------------------------------------------------------------------

def _fqcn(c: Class) -> str:
    return f"{c.package}.{c.name}" if c.package else c.name


def _medoid(member_sigs: Sequence[int]) -> tuple[int, int]:
    """`(index, max_dist)` of the member minimizing total Hamming distance to
    the others (ties: lowest signature). The mode of an exact-duplicate-heavy
    cluster wins naturally (zero distance to its copies)."""
    pool = range(min(len(member_sigs), _MEDOID_CAP))
    best_i, best_total = 0, None
    for i in pool:
        total = sum((member_sigs[i] ^ s).bit_count() for s in member_sigs)
        key = (total, member_sigs[i])
        if best_total is None or key < best_total:
            best_i, best_total = i, key
    max_dist = max((member_sigs[best_i] ^ s).bit_count() for s in member_sigs)
    return best_i, max_dist


def build_family(
    samples: Sequence[FamilySample],
    *,
    name: str,
    k: Optional[int] = None,
    radius: int = DEFAULT_BUILD_RADIUS,
    min_instructions: int = DEFAULT_MIN_INSTRUCTIONS,
) -> FamilySignature:
    """The k-of-N shared structure of `samples` (already filtered/deduped).
    `k=None` means all N. Requires `twinflame_libsigs` (raises ImportError)."""
    try:
        from twinflame_libsigs.cluster import cluster_by_radius
    except ImportError as e:
        raise ImportError(_LIBSIGS_HINT) from e
    from twinflame_libsigs.strings import useful_strings

    n = len(samples)
    if n < 2:
        raise ValueError("a family needs at least 2 samples")
    k = n if k is None else k
    if not 1 <= k <= n:
        raise ValueError(f"k must be in 1..{n} (got {k})")

    sigs: list[int] = []
    owner_sample: list[int] = []
    owner_class: list[Class] = []
    for s in samples:
        for c in s.classes:
            sigs.append((c.signature or compute_signature(c)).combined)
            owner_sample.append(s.sample_id)
            owner_class.append(c)

    entries: list[FamilyEntry] = []
    for group in cluster_by_radius(sigs, radius=radius):
        sample_ids = tuple(sorted({owner_sample[i] for i in group}))
        if len(sample_ids) < k:
            continue
        group = sorted(group, key=lambda i: sigs[i])  # deterministic medoid
        member_sigs = [sigs[i] for i in group]
        mi, max_dist = _medoid(member_sigs)
        medoid_class = owner_class[group[mi]]
        shared = set(useful_strings(medoid_class.strings))
        for i in group:
            if not shared:
                break
            shared &= useful_strings(owner_class[i].strings)
        strings = tuple(sorted(shared, key=lambda s: (-len(s), s))[:MAX_ENTRY_STRINGS])
        entries.append(FamilyEntry(
            signature=member_sigs[mi],
            fqcn=_fqcn(medoid_class),
            strings=strings,
            instructions=medoid_class.total_instructions,
            coverage=len(sample_ids),
            sample_ids=sample_ids,
            max_dist=max_dist,
        ))
    entries.sort(key=lambda e: (-e.coverage, -e.instructions, e.signature))
    return FamilySignature(
        name=name,
        n_samples=n,
        k=k,
        radius_build=radius,
        min_instructions=min_instructions,
        samples=tuple((s.label, s.digest) for s in samples),
        entries=tuple(entries),
    )


def save_family(fam: FamilySignature, path: Path, *, extra_meta: Optional[dict] = None) -> None:
    """Write the family as a `.tflp` pack (+ `.idx.json` sidecar), stamped with
    the running `SIGNATURE_STAMP`."""
    try:
        from twinflame_libsigs.pack import write_pack
    except ImportError as e:
        raise ImportError(_LIBSIGS_HINT) from e
    from .prepare import SIGNATURE_STAMP

    stream = [(i, e.signature, e.strings) for i, e in enumerate(fam.entries)]
    sidecar = {
        str(i): {
            "fqcn": e.fqcn,
            "coverage": e.coverage,
            "samples": list(e.sample_ids),
            "instructions": e.instructions,
            "max_dist": e.max_dist,
        }
        for i, e in enumerate(fam.entries)
    }
    meta = {
        "kind": PACK_KIND,
        "family": fam.name,
        "n_samples": fam.n_samples,
        "k": fam.k,
        "radius_build": fam.radius_build,
        "min_instr": fam.min_instructions,
        "samples": [{"label": lb, "digest": dg} for lb, dg in fam.samples],
    }
    if extra_meta:
        meta.update(extra_meta)
    write_pack(path, stream, sidecar, sig_stamp=SIGNATURE_STAMP, meta=meta)


# --- match ----------------------------------------------------------------------

def load_family_detector(pack: Path, *, radius: int):
    """`(detector, meta, payloads)` for a family pack. Stale or inconsistent
    packs raise (`StaleStoreError` / `ValueError`) — a family verdict must not
    silently come from a bad pack, unlike libdict's optional labeling."""
    try:
        from twinflame_libsigs.detect import LibraryDetector
        from twinflame_libsigs.pack import read_meta, read_pack
    except ImportError as e:
        raise ImportError(_LIBSIGS_HINT) from e
    from .prepare import SIGNATURE_STAMP

    entries, payloads, _ = read_pack(pack, expect_stamp=SIGNATURE_STAMP)
    detector = LibraryDetector.build(entries, radius=radius)
    return detector, read_meta(pack), payloads


def match_family(
    detector,
    meta: dict,
    payloads: Dict[str, dict],
    candidate: Iterable[Class],
    *,
    pack: str = "",
    use_strings: bool = True,
    probe_min_instructions: int = DEFAULT_MIN_INSTRUCTIONS,
) -> FamilyMatchResult:
    """Containment of the family in `candidate`: every candidate class probes
    the detector; each family entry counts as present through its best hit
    (lowest tier, then distance). Score weights entries by
    `(instructions + 1) * coverage`, so big classes shared by every sample
    dominate small ones that barely cleared k."""
    best: Dict[int, tuple[tuple[int, int], Class]] = {}   # pid -> ((tier, dist), class)
    for c in candidate:
        if c.total_instructions < probe_min_instructions:
            continue
        sig = c.signature or compute_signature(c)
        hit = detector.detect(sig.combined, c.strings)
        if hit is None or (not use_strings and hit.tier == 3):
            continue
        key = (hit.tier, hit.distance)
        prev = best.get(hit.payload_id)
        if prev is None or key < prev[0]:
            best[hit.payload_id] = (key, c)

    def weight(p: dict) -> int:
        return (p.get("instructions", 0) + 1) * p.get("coverage", 1)

    weight_total = sum(weight(p) for p in payloads.values())
    weight_present = sum(weight(payloads[str(pid)]) for pid in best if str(pid) in payloads)
    by_tier: Dict[int, int] = {}
    evidence = []
    for pid, ((tier, dist), c) in best.items():
        by_tier[tier] = by_tier.get(tier, 0) + 1
        fqcn = payloads.get(str(pid), {}).get("fqcn", f"payload/{pid}")
        evidence.append((fqcn, c.descriptor, tier, dist))
    evidence.sort(key=lambda e: (e[2], e[3], e[0]))
    return FamilyMatchResult(
        family=meta.get("family", "?"),
        pack=pack,
        score=(weight_present / weight_total) if weight_total else 0.0,
        present=len(best),
        total=len(payloads),
        weight_present=weight_present,
        weight_total=weight_total,
        by_tier=by_tier,
        evidence=tuple(evidence),
    )
