"""Change classifier + report — change-detection Phase 2.

Turns the matcher's `Match` list into a **typed, ranked, machine-consumable**
change report: the actual deliverable (UC1 "what changed between two versions").
Consumes the Phase-1 semantic feature layer (`features.class_change`) so the
verdict rests on *semantic-feature deltas* (framework calls / strings / types),
not the structural distance — which also moves on re-obfuscation noise.

Verdict per class:
- **added**    — only in the newer build (new code).
- **removed**  — only in the older build.
- **modified** — matched, and the semantic features differ (real edit).
- **cosmetic** — matched, structurally not identical, but *no* semantic-feature
  change → re-obfuscation / re-optimization noise. Emitted, but demoted.
- **unchanged**— matched and structurally identical.

Design (per the UC discussion, roadmap E-3): the matcher is precision-biased
(an unsure pair is left added/removed, visible on both sides); this classifier is
recall-biased-but-**scored** — every class is emitted with a `magnitude` and
category, nothing is silently dropped, and the *consumer* thresholds. Cosmetic/
unchanged are ranked last ("demote, never drop"). Machine-consumable JSON + a
human text summary; no interactive UX (external tools visualise via the mapping).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from typing import Optional

from .features import ClassMap, FeatureDelta, class_change
from .model import Class, Match

# Review-worthiness ordering: real edits first, cosmetic/unchanged last.
_KIND_RANK = {"modified": 0, "added": 1, "removed": 2, "cosmetic": 3, "unchanged": 4}
# Structural distance at/above which a paired class with no semantic delta is
# "unchanged" rather than "cosmetic" (float-exactness slack).
_IDENTICAL = 1.0 - 1e-9


@dataclass(frozen=True, slots=True)
class ClassChange:
    kind: str
    lhs: Optional[str]          # older-build class descriptor (None if added)
    rhs: Optional[str]          # newer-build class descriptor (None if removed)
    source_file: Optional[str]  # best available human hint (older side preferred)
    magnitude: int              # review-worthiness (feature-delta count / size)
    match_distance: float       # structural distance of the underlying match
    anchored: bool              # match rests on a string/framework-call anchor
    delta: Optional[FeatureDelta]  # only for "modified"

    @property
    def summary(self) -> str:
        if self.kind == "modified" and self.delta is not None:
            return self.delta.summary()
        if self.kind in ("added", "removed"):
            return f"{self.magnitude} instructions"
        return self.kind


def _size(c: Class) -> int:
    return c.total_instructions


def build_class_map(matches: Iterable[Match]) -> dict[str, str]:
    """A-descriptor -> B-descriptor over the matched (non-added/removed) pairs.

    Threaded into `class_change` so *app*-type dependency deltas become
    comparable across the rename boundary (see `features.diff_features`)."""
    return {
        m.lhs.descriptor: m.rhs.descriptor
        for m in matches
        if not m.is_added and not m.is_deleted
    }


def classify_match(m: Match, class_map: Optional[ClassMap] = None) -> ClassChange:
    """One `Match` -> one typed change verdict."""
    if m.is_added:
        return ClassChange("added", None, m.rhs.descriptor, m.rhs.source_file,
                           _size(m.rhs), 0.0, False, None)
    if m.is_deleted:
        return ClassChange("removed", m.lhs.descriptor, None, m.lhs.source_file,
                           _size(m.lhs), 0.0, False, None)

    delta = class_change(m.lhs, m.rhs, class_map)
    anchored = m.breakdown.get("anchored") == 1.0
    src = m.lhs.source_file or m.rhs.source_file
    if delta.is_semantic:
        return ClassChange("modified", m.lhs.descriptor, m.rhs.descriptor, src,
                           delta.magnitude, m.distance, anchored, delta)
    kind = "unchanged" if m.distance >= _IDENTICAL else "cosmetic"
    return ClassChange(kind, m.lhs.descriptor, m.rhs.descriptor, src,
                       0, m.distance, anchored, None)


def change_set(matches: Iterable[Match]) -> list[ClassChange]:
    """All verdicts, ranked most-review-worthy first (stable within a kind by
    descending magnitude, then source file for determinism)."""
    matches = list(matches)
    class_map = build_class_map(matches)
    changes = [classify_match(m, class_map) for m in matches]
    changes.sort(key=lambda c: (_KIND_RANK.get(c.kind, 9), -c.magnitude,
                                c.source_file or "", c.lhs or c.rhs or ""))
    return changes


def counts(changes: Iterable[ClassChange]) -> dict[str, int]:
    return dict(Counter(c.kind for c in changes))


def render_json(changes: Iterable[ClassChange]) -> str:
    """Machine-consumable report. Full deltas included for modified classes so a
    downstream tool can localise the change without re-running the diff."""
    changes = list(changes)
    rows = []
    for c in changes:
        row = {
            "kind": c.kind,
            "lhs": c.lhs,
            "rhs": c.rhs,
            "source_file": c.source_file,
            "magnitude": c.magnitude,
            "match_distance": round(c.match_distance, 4),
            "anchored": c.anchored,
        }
        if c.delta is not None:
            row["delta"] = {
                "calls_added": list(c.delta.calls_added),
                "calls_removed": list(c.delta.calls_removed),
                "strings_added": list(c.delta.strings_added),
                "strings_removed": list(c.delta.strings_removed),
                "types_added": list(c.delta.refs_added),
                "types_removed": list(c.delta.refs_removed),
                "app_types_added": list(c.delta.app_refs_added),
                "app_types_removed": list(c.delta.app_refs_removed),
            }
        rows.append(row)
    return json.dumps({"summary": counts(changes), "changes": rows},
                      indent=2, sort_keys=True)


def render_text(changes: Iterable[ClassChange], *, top: int = 30) -> str:
    """Human triage summary: counts, then the top modified/added/removed."""
    changes = list(changes)
    c = counts(changes)
    lines = [
        "change summary: "
        + "  ".join(f"{k}={c.get(k, 0)}" for k in
                    ("modified", "added", "removed", "cosmetic", "unchanged")),
    ]
    modified = [x for x in changes if x.kind == "modified"][:top]
    if modified:
        lines.append(f"\ntop modified (by magnitude):")
        for x in modified:
            src = x.source_file or x.lhs or "?"
            anc = " [anchored]" if x.anchored else ""
            lines.append(f"  [{x.magnitude:4d}] {src}  {x.summary}{anc}")
    added = [x for x in changes if x.kind == "added"][:top]
    if added:
        lines.append(f"\ntop added (by size):")
        for x in added:
            lines.append(f"  [{x.magnitude:4d} instr] {x.source_file or x.rhs}")
    removed = [x for x in changes if x.kind == "removed"][:top]
    if removed:
        lines.append(f"\ntop removed (by size):")
        for x in removed:
            lines.append(f"  [{x.magnitude:4d} instr] {x.source_file or x.lhs}")
    return "\n".join(lines)
