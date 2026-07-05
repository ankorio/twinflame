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

from typing import Iterable as _Iterable, Optional, Union

from .features import (
    ClassMap,
    FeatureDelta,
    MethodDelta,
    class_change,
    localize_method_changes,
)
from .model import Class, Match
from .provenance import ORIGIN_RANK, dev_descriptor_prefixes, origin_of

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
    origin: str                 # "app" / "library" / "unknown" (review priority)
    delta: Optional[FeatureDelta]  # only for "modified"
    method_deltas: tuple[MethodDelta, ...] = ()  # per-method localization (modified)

    @property
    def summary(self) -> str:
        if self.kind == "modified":
            parts = []
            if self.delta is not None and self.delta.is_semantic:
                parts.append(self.delta.summary())
            n_add = sum(1 for md in self.method_deltas if md.status == "added")
            n_del = sum(1 for md in self.method_deltas if md.status == "deleted")
            if n_add:
                parts.append(f"+{n_add} methods")
            if n_del:
                parts.append(f"-{n_del} methods")
            return ", ".join(parts) if parts else "structure changed"
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


def classify_match(
    m: Match,
    class_map: Optional[ClassMap] = None,
    dev_prefix: Union[str, tuple[str, ...], None] = None,
) -> ClassChange:
    """One `Match` -> one typed change verdict. `dev_prefix` (a descriptor prefix
    or tuple of them, from `provenance.dev_descriptor_prefixes`) enables
    app/library origin tagging."""
    if m.is_added:
        return ClassChange("added", None, m.rhs.descriptor, m.rhs.source_file,
                           _size(m.rhs), 0.0, False, origin_of(m.rhs, dev_prefix), None)
    if m.is_deleted:
        return ClassChange("removed", m.lhs.descriptor, None, m.lhs.source_file,
                           _size(m.lhs), 0.0, False, origin_of(m.lhs, dev_prefix), None)

    delta = class_change(m.lhs, m.rhs, class_map)
    anchored = m.breakdown.get("anchored") == 1.0
    src = m.lhs.source_file or m.rhs.source_file
    origin = origin_of(m.lhs, dev_prefix)  # older side names the class we review
    method_deltas = localize_method_changes(m.method_matches)
    # A method added/removed is a real structural change even if it moved no
    # class-level semantic feature (e.g. a new method with no framework calls) —
    # so it counts as "modified", not cosmetic. (A modified method whose call
    # set didn't move is *not* localized and stays cosmetic; see
    # localize_method_changes.)
    n_structural = sum(1 for md in method_deltas if md.status in ("added", "deleted"))
    if delta.is_semantic or n_structural:
        return ClassChange("modified", m.lhs.descriptor, m.rhs.descriptor, src,
                           delta.magnitude + n_structural, m.distance, anchored,
                           origin, delta, method_deltas)
    kind = "unchanged" if m.distance >= _IDENTICAL else "cosmetic"
    return ClassChange(kind, m.lhs.descriptor, m.rhs.descriptor, src,
                       0, m.distance, anchored, origin, None)


def change_set(
    matches: Iterable[Match],
    dev_package: Union[str, _Iterable[str], None] = None,
) -> list[ClassChange]:
    """All verdicts, ranked most-review-worthy first: by kind (real edits first),
    then **origin** (the developer's own `app` code above `library` churn), then
    descending magnitude, then source file for determinism. `dev_package` — one
    prefix or several (multi-root apps), e.g. from `--app-package`/`--package` or
    the manifest — drives the app/library split; without it only known libraries
    are demoted."""
    matches = list(matches)
    class_map = build_class_map(matches)
    dev_prefix = dev_descriptor_prefixes(dev_package)
    changes = [classify_match(m, class_map, dev_prefix) for m in matches]
    changes.sort(key=lambda c: (_KIND_RANK.get(c.kind, 9),
                                ORIGIN_RANK.get(c.origin, 1), -c.magnitude,
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
            "origin": c.origin,
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
        if c.method_deltas:
            row["methods"] = [
                {
                    "status": md.status,
                    "name": md.name,
                    "descriptor": md.descriptor,
                    "instr_count": md.instr_count,
                    "calls_added": list(md.calls_added),
                    "calls_removed": list(md.calls_removed),
                }
                for md in c.method_deltas
            ]
        rows.append(row)
    return json.dumps({"summary": counts(changes), "changes": rows},
                      indent=2, sort_keys=True)


_ORIGIN_TAG = {"app": "app", "library": "lib", "unknown": "?"}


def render_text(changes: Iterable[ClassChange], *, top: int = 30) -> str:
    """Human triage summary: counts (with an app-only breakdown), then the top
    modified/added/removed — ordered app-first, each tagged with its origin."""
    changes = list(changes)
    c = counts(changes)
    app = counts([x for x in changes if x.origin == "app"])
    any_app = any(x.origin == "app" for x in changes)
    lines = [
        "change summary: "
        + "  ".join(f"{k}={c.get(k, 0)}" for k in
                    ("modified", "added", "removed", "cosmetic", "unchanged")),
    ]
    if any_app:
        lines.append(
            "  app-only:     "
            + "  ".join(f"{k}={app.get(k, 0)}" for k in
                        ("modified", "added", "removed", "cosmetic", "unchanged"))
        )

    def _tag(x: ClassChange) -> str:
        return f"[{_ORIGIN_TAG.get(x.origin, '?'):>3}]"

    modified = [x for x in changes if x.kind == "modified"][:top]
    if modified:
        lines.append("\ntop modified (app-first, then by magnitude):")
        for x in modified:
            src = x.source_file or x.lhs or "?"
            anc = " [anchored]" if x.anchored else ""
            lines.append(f"  {_tag(x)} [{x.magnitude:4d}] {src}  {x.summary}{anc}")
            for md in x.method_deltas[:3]:  # localize to the top changed methods
                lines.append(f"          - {md.name}{md.descriptor}: {md.summary()}")
    added = [x for x in changes if x.kind == "added"][:top]
    if added:
        lines.append("\ntop added (app-first, then by size):")
        for x in added:
            lines.append(f"  {_tag(x)} [{x.magnitude:4d} instr] {x.source_file or x.rhs}")
    removed = [x for x in changes if x.kind == "removed"][:top]
    if removed:
        lines.append("\ntop removed (app-first, then by size):")
        for x in removed:
            lines.append(f"  {_tag(x)} [{x.magnitude:4d} instr] {x.source_file or x.lhs}")
    return "\n".join(lines)
