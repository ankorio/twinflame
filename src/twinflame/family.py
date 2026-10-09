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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

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
    # Same hits keyed by payload id, for callers that need the entry identity
    # rather than its (possibly duplicated) name: (payload_id, fqcn, cand desc,
    # tier, dist), in `evidence` order.
    hits: Tuple[Tuple[int, str, str, int, int], ...] = ()
    # Per hit, keyed by payload id: the detector's evidence score (1.0 for a
    # signature hit, the summed IDF of the shared strings for a tier-3 hit)
    # and the distinctive strings the candidate shares with the entry (filled
    # only when `match_family` is given `entry_strings`).
    hit_scores: Dict[int, float] = field(default_factory=dict)
    hit_strings: Dict[int, Tuple[str, ...]] = field(default_factory=dict)


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


def save_family(
    fam: FamilySignature,
    path: Path,
    *,
    extra_meta: Optional[dict] = None,
    payload_extra: Optional[Dict[int, dict]] = None,
) -> None:
    """Write the family as a `.tflp` pack (+ `.idx.json` sidecar), stamped with
    the running `SIGNATURE_STAMP`. `payload_extra[i]` is merged into entry
    `i`'s sidecar record (caller-owned keys such as notes); it cannot override
    the fixed keys."""
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
    for i, extra in (payload_extra or {}).items():
        rec = sidecar.get(str(i))
        if rec is None:
            raise ValueError(f"payload_extra refers to entry {i}, pack has {len(fam.entries)}")
        rec.update({k: v for k, v in extra.items() if k not in rec})
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


def build_curated_pack(
    classes: Sequence[Class],
    *,
    name: str,
    sample_ids: Optional[Sequence[int]] = None,
    samples: Sequence[Tuple[str, str]] = (),
    radius: int = DEFAULT_BUILD_RADIUS,
    min_instructions: int = DEFAULT_MIN_INSTRUCTIONS,
) -> FamilySignature:
    """A hand-curated family: one entry per given class, in the given order
    (so entry id == position), no clustering and no filtering — the analyst
    already chose. `sample_ids[i]` is the index into `samples` the class came
    from (all 0 when omitted). `radius` is stored as `radius_build`, which
    `family match` uses as its default match radius, so pass the drift you
    expect for this family. Needs `twinflame_libsigs` for `useful_strings`."""
    try:
        from twinflame_libsigs.strings import useful_strings
    except ImportError as e:
        raise ImportError(_LIBSIGS_HINT) from e
    if not classes:
        raise ValueError("a curated pack needs at least one class")
    if sample_ids is not None and len(sample_ids) != len(classes):
        raise ValueError("sample_ids must be parallel to classes")
    samples = tuple(samples) or (("curated", ""),)
    entries = []
    for i, c in enumerate(classes):
        sid = sample_ids[i] if sample_ids is not None else 0
        if not 0 <= sid < len(samples):
            raise ValueError(f"sample id {sid} out of range for {len(samples)} sample(s)")
        strings = tuple(sorted(useful_strings(c.strings),
                               key=lambda s: (-len(s), s))[:MAX_ENTRY_STRINGS])
        entries.append(FamilyEntry(
            signature=(c.signature or compute_signature(c)).combined,
            fqcn=_fqcn(c),
            strings=strings,
            instructions=c.total_instructions,
            coverage=1,
            sample_ids=(sid,),
            max_dist=0,
        ))
    return FamilySignature(
        name=name,
        n_samples=len(samples),
        k=1,
        radius_build=radius,
        min_instructions=min_instructions,
        samples=tuple(samples),
        entries=tuple(entries),
    )


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
    entry_strings: Optional[Mapping[int, Sequence[str]]] = None,
) -> FamilyMatchResult:
    """Containment of the family in `candidate`: every candidate class probes
    the detector; each family entry counts as present through its best hit
    (lowest tier, then distance). Score weights entries by
    `(instructions + 1) * coverage`, so big classes shared by every sample
    dominate small ones that barely cleared k. With `entry_strings` (payload
    id -> the entry's strings) every hit also reports the distinctive strings
    the two classes share, so a caller can show *why* a tier-3 hit fired."""
    best: Dict[int, tuple[tuple[int, int], Class, float]] = {}   # pid -> ((tier, dist), class, score)
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
            best[hit.payload_id] = (key, c, hit.score)

    def weight(p: dict) -> int:
        return (p.get("instructions", 0) + 1) * p.get("coverage", 1)

    weight_total = sum(weight(p) for p in payloads.values())
    weight_present = sum(weight(payloads[str(pid)]) for pid in best if str(pid) in payloads)
    by_tier: Dict[int, int] = {}
    hits = []
    hit_scores: Dict[int, float] = {}
    hit_strings: Dict[int, Tuple[str, ...]] = {}
    for pid, ((tier, dist), c, score) in best.items():
        by_tier[tier] = by_tier.get(tier, 0) + 1
        fqcn = payloads.get(str(pid), {}).get("fqcn", f"payload/{pid}")
        hits.append((pid, fqcn, c.descriptor, tier, dist))
        hit_scores[pid] = float(score)
        if entry_strings is not None:
            hit_strings[pid] = shared_strings(c.strings, entry_strings.get(pid, ()))
    hits.sort(key=lambda h: (h[3], h[4], h[1], h[0]))
    evidence = [(fqcn, desc, tier, dist) for _, fqcn, desc, tier, dist in hits]
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
        hits=tuple(hits),
        hit_scores=hit_scores,
        hit_strings=hit_strings,
    )


def shared_strings(candidate: Iterable[str], entry: Iterable[str]) -> Tuple[str, ...]:
    """Distinctive strings two classes have in common — the evidence behind a
    tier-3 hit — longest first."""
    from twinflame_libsigs.strings import useful_strings
    common = useful_strings(candidate) & useful_strings(entry)
    return tuple(sorted(common, key=lambda s: (-len(s), s)))
