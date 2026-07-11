"""Known-library dictionary — optional `twinflame_libsigs` integration.

When a catalogue pack (built offline by `twinflame-libsigs scrape` + `build`)
is available, app classes whose signature/strings match a known library release
get an evidence-based provenance label `<coord>@<version-range>` — replacing
guesswork for the classes R8 renamed out of `provenance.LIBRARY_PREFIXES`'s
reach. The labels feed:

- the change report (renamed library churn demoted below app code),
- containment scoring (labeled classes excluded from the family side).

The dependency is deliberately soft: `twinflame_libsigs` requires twinflame,
so core only ever imports it lazily, and everything here degrades to "no
labels" (with a stderr note) when the package or the pack is missing or stale.

Detection defaults are the corpus-validated operating point (P 0.83 / R 0.65):
Hamming radius 4, classes under 20 instructions never probed, and a library
must be detected in `min_classes` distinct app classes before its per-class
labels are believed (library-level aggregation — far more robust than
trusting any single class hit).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Optional

from .model import Class
from .signature import compute_signature

# Corpus-validated detection defaults (see the libsigs design + findings).
DEFAULT_RADIUS = 4
MIN_INSTRUCTIONS = 20
MIN_CLASSES_PER_LIBRARY = 5

ENV_PACK = "TWINFLAME_LIBSIGS"
DEFAULT_PACK_PATH = Path.home() / ".cache" / "twinflame" / "libsigs.tflp"


def find_pack(explicit: Optional[str] = None) -> Optional[Path]:
    """Resolve the catalogue pack: explicit path > $TWINFLAME_LIBSIGS >
    ~/.cache/twinflame/libsigs.tflp. None when nothing exists."""
    for cand in (explicit, os.environ.get(ENV_PACK), DEFAULT_PACK_PATH):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def load_detector(pack: Path, *, radius: int = DEFAULT_RADIUS, log=None):
    """A ready `LibraryDetector` for `pack`, or None (with a `log` note) when
    twinflame_libsigs isn't installed or the pack predates the current
    signature algorithm."""
    try:
        from twinflame_libsigs.detect import LibraryDetector
        from twinflame_libsigs.store import StaleStoreError
    except ImportError:
        if log:
            log("libsigs: pack found but twinflame_libsigs is not installed; "
                "skipping library labeling")
        return None
    from .prepare import SIGNATURE_STAMP

    try:
        return LibraryDetector.from_pack(pack, radius=radius,
                                         expect_stamp=SIGNATURE_STAMP)
    except StaleStoreError as e:
        if log:
            log(f"libsigs: {e}; skipping library labeling")
        return None


def label_classes(
    classes: Iterable[Class],
    detector,
    *,
    min_instr: int = MIN_INSTRUCTIONS,
    min_classes: int = MIN_CLASSES_PER_LIBRARY,
) -> Dict[str, str]:
    """descriptor -> `<coord>@<ranges>` for every class the dictionary
    recognises. Two passes: detect per class, then keep only libraries seen in
    at least `min_classes` distinct classes (per-class hits for a library that
    appears once or twice are the collision noise the aggregation exists to
    kill). Signatures are computed on demand for freshly-parsed classes."""
    hits: Dict[str, tuple[str, str]] = {}   # descriptor -> (coord, label)
    per_coord: Dict[str, int] = {}
    for c in classes:
        if c.total_instructions < min_instr:
            continue
        sig = c.signature or compute_signature(c)
        det = detector.detect(sig.combined, c.strings)
        if det is None:
            continue
        meta = detector.resolve(det)
        if meta is None:
            continue
        coord = meta["coord"]
        ranges = ",".join(meta.get("ranges", ())) or "?"
        hits[c.descriptor] = (coord, f"{coord}@{ranges}")
        per_coord[coord] = per_coord.get(coord, 0) + 1
    return {
        desc: label
        for desc, (coord, label) in hits.items()
        if per_coord[coord] >= min_classes
    }


def summarize(labels: Dict[str, str]) -> str:
    """One stderr line: how many classes across how many libraries."""
    coords = {label.split("@", 1)[0] for label in labels.values()}
    return f"{len(labels)} classes across {len(coords)} libraries"
