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

# Bump on any breaking change to the JSON shape (see the Change-Report-Schema wiki page).
# v2: added superclass / interfaces / components on paired rows (feedback #4).
# v3: added `library` (dictionary-evidence label `<coord>@<version-range>`).
CHANGES_SCHEMA_VERSION = 3

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
    # "high" | "low". A modified verdict is "low" when it rests *only* on
    # framework-call churn in non-app code — the cross-toolchain-noise signature
    # (different R8 versions relocate calls between classes; measured 95% of the
    # false-modified on a same-source cross-R8 rebuild). Everything else is "high".
    confidence: str = "high"
    # Type-graph context for the reviewer (feedback #4). Framework superclass /
    # interface descriptors survive R8, so they carry across the rename boundary.
    lhs_super: Optional[str] = None
    rhs_super: Optional[str] = None
    lhs_interfaces: tuple[str, ...] = ()
    rhs_interfaces: tuple[str, ...] = ()
    # Sensitive-component short-codes implemented by this class on either side
    # (e.g. ("BAS",) for an AccessibilityService). See components.py.
    components: tuple[str, ...] = ()
    # Dictionary-evidence provenance: `<coord>@<version-range>` when the class
    # matched the known-library signature dictionary (libdict.py). Present even
    # for renamed classes the prefix heuristics can't reach.
    library: Optional[str] = None

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


def _components(*descriptors: Optional[str], component_map: Optional[dict[str, set[str]]]) -> tuple[str, ...]:
    if not component_map:
        return ()
    codes: set[str] = set()
    for d in descriptors:
        if d:
            codes |= component_map.get(d, set())
    return tuple(sorted(codes))


def _library_evidence(
    library_map: Optional[dict[str, str]], *descriptors: Optional[str]
) -> Optional[str]:
    """The dictionary label for the first descriptor that has one, or None."""
    if not library_map:
        return None
    for d in descriptors:
        if d and (label := library_map.get(d)):
            return label
    return None


def _apply_library(origin: str, label: Optional[str]) -> str:
    """Dictionary evidence demotes a class to `library` — unless the dev prefix
    claimed it as `app` (the documented priority: an app deliberately vendoring
    a library under its own package still reviews as app code)."""
    return "library" if label and origin != "app" else origin


def classify_match(
    m: Match,
    class_map: Optional[ClassMap] = None,
    dev_prefix: Union[str, tuple[str, ...], None] = None,
    component_map: Optional[dict[str, set[str]]] = None,
    library_map: Optional[dict[str, str]] = None,
) -> ClassChange:
    """One `Match` -> one typed change verdict. `dev_prefix` (a descriptor prefix
    or tuple of them, from `provenance.dev_descriptor_prefixes`) enables
    app/library origin tagging. `component_map` (descriptor -> sensitive-component
    codes, from `components.component_labels`) tags dangerous base classes.
    `library_map` (descriptor -> `<coord>@<range>`, from `libdict.label_classes`)
    adds dictionary-evidence library labels that survive R8 renames."""
    if m.is_added:
        lib = _library_evidence(library_map, m.rhs.descriptor)
        return ClassChange("added", None, m.rhs.descriptor, m.rhs.source_file,
                           _size(m.rhs), 0.0, False,
                           _apply_library(origin_of(m.rhs, dev_prefix), lib), None,
                           rhs_super=m.rhs.superclass, rhs_interfaces=m.rhs.interfaces,
                           components=_components(m.rhs.descriptor, component_map=component_map),
                           library=lib)
    if m.is_deleted:
        lib = _library_evidence(library_map, m.lhs.descriptor)
        return ClassChange("removed", m.lhs.descriptor, None, m.lhs.source_file,
                           _size(m.lhs), 0.0, False,
                           _apply_library(origin_of(m.lhs, dev_prefix), lib), None,
                           lhs_super=m.lhs.superclass, lhs_interfaces=m.lhs.interfaces,
                           components=_components(m.lhs.descriptor, component_map=component_map),
                           library=lib)

    delta = class_change(m.lhs, m.rhs, class_map)
    lib = _library_evidence(library_map, m.lhs.descriptor, m.rhs.descriptor)
    type_ctx = dict(
        lhs_super=m.lhs.superclass, rhs_super=m.rhs.superclass,
        lhs_interfaces=m.lhs.interfaces, rhs_interfaces=m.rhs.interfaces,
        components=_components(m.lhs.descriptor, m.rhs.descriptor, component_map=component_map),
        library=lib,
    )
    anchored = m.breakdown.get("anchored") == 1.0
    src = m.lhs.source_file or m.rhs.source_file
    # Older side names the class we review; dictionary evidence beats prefixes.
    origin = _apply_library(origin_of(m.lhs, dev_prefix), lib)
    method_deltas = localize_method_changes(m.method_matches)
    # A method added/removed is a real structural change even if it moved no
    # class-level semantic feature (e.g. a new method with no framework calls) —
    # so it counts as "modified", not cosmetic. (A modified method whose call
    # set didn't move is *not* localized and stays cosmetic; see
    # localize_method_changes.)
    n_structural = sum(1 for md in method_deltas if md.status in ("added", "deleted"))
    if delta.is_semantic or n_structural:
        # Confidence: content-backed (string/type) or structural (method add/del)
        # deltas are toolchain-stable; a *call-only* delta in non-app code is the
        # cross-R8-version noise signature, so demote it to "low".
        content_backed = bool(
            delta.strings_added or delta.strings_removed
            or delta.refs_added or delta.refs_removed
            or delta.app_refs_added or delta.app_refs_removed
        )
        call_only = not content_backed and n_structural == 0
        confidence = "low" if (call_only and origin != "app") else "high"
        return ClassChange("modified", m.lhs.descriptor, m.rhs.descriptor, src,
                           delta.magnitude + n_structural, m.distance, anchored,
                           origin, delta, method_deltas, confidence, **type_ctx)
    kind = "unchanged" if m.distance >= _IDENTICAL else "cosmetic"
    return ClassChange(kind, m.lhs.descriptor, m.rhs.descriptor, src,
                       0, m.distance, anchored, origin, None, **type_ctx)


def change_set(
    matches: Iterable[Match],
    dev_package: Union[str, _Iterable[str], None] = None,
    component_map: Optional[dict[str, set[str]]] = None,
    library_map: Optional[dict[str, str]] = None,
) -> list[ClassChange]:
    """All verdicts, ranked most-review-worthy first: by kind (real edits first),
    then **origin** (the developer's own `app` code above `library` churn), then
    descending magnitude, then source file for determinism. `dev_package` — one
    prefix or several (multi-root apps), e.g. from `--app-package`/`--package` or
    the manifest — drives the app/library split; without it only known libraries
    are demoted. `component_map` tags sensitive base classes (see components.py).
    `library_map` (from `libdict.label_classes`) demotes dictionary-recognised
    library classes even when R8 renamed them out of the prefix heuristics."""
    matches = list(matches)
    class_map = build_class_map(matches)
    dev_prefix = dev_descriptor_prefixes(dev_package)
    changes = [classify_match(m, class_map, dev_prefix, component_map, library_map)
               for m in matches]
    changes.sort(key=lambda c: (_KIND_RANK.get(c.kind, 9),
                                ORIGIN_RANK.get(c.origin, 1),
                                0 if c.confidence == "high" else 1, -c.magnitude,
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
            "library": c.library,
            "confidence": c.confidence,
            "lhs_super": c.lhs_super,
            "rhs_super": c.rhs_super,
            "lhs_interfaces": list(c.lhs_interfaces),
            "rhs_interfaces": list(c.rhs_interfaces),
            "components": list(c.components),
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
    doc = {
        "schema_version": CHANGES_SCHEMA_VERSION,
        "summary": counts(changes),
        "changes": rows,
    }
    return json.dumps(doc, indent=2, sort_keys=True)


_CSV_COLUMNS = (
    "kind", "origin", "library", "confidence", "lhs", "rhs", "magnitude",
    "match_distance", "anchored", "lhs_super", "rhs_super",
    "interfaces", "components", "summary",
)


def _row_values(c: ClassChange) -> dict[str, str]:
    ifaces = sorted(set(c.lhs_interfaces) | set(c.rhs_interfaces))
    return {
        "kind": c.kind,
        "origin": c.origin,
        "library": c.library or "",
        "confidence": c.confidence,
        "lhs": c.lhs or "",
        "rhs": c.rhs or "",
        "magnitude": str(c.magnitude),
        "match_distance": f"{c.match_distance:.4f}",
        "anchored": "true" if c.anchored else "false",
        "lhs_super": c.lhs_super or "",
        "rhs_super": c.rhs_super or "",
        "interfaces": " ".join(ifaces),
        "components": ";".join(c.components),
        "summary": c.summary,
    }


def render_csv(changes: Iterable[ClassChange]) -> str:
    """Flat, spreadsheet/grep-friendly view: one row per class change."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for c in changes:
        w.writerow(_row_values(c))
    return buf.getvalue()


def render_xml(changes: Iterable[ClassChange]) -> str:
    """Same per-class data as XML for pipelines that consume it."""
    import xml.etree.ElementTree as ET

    changes = list(changes)
    root = ET.Element("twinflame", attrib={"schema_version": str(CHANGES_SCHEMA_VERSION)})
    summ = ET.SubElement(root, "summary")
    for k, v in counts(changes).items():
        ET.SubElement(summ, "count", attrib={"kind": k}).text = str(v)
    body = ET.SubElement(root, "changes")
    for c in changes:
        row = _row_values(c)
        el = ET.SubElement(body, "change")
        for key, val in row.items():
            if val:
                ET.SubElement(el, key).text = val
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"


_SELECT_KINDS = {
    "all": None,
    "changed": {"modified", "added", "removed"},
    "unchanged": {"unchanged", "cosmetic"},
}


def select_changes(changes: Iterable[ClassChange], mode: str) -> list[ClassChange]:
    """Filter to only-changed ("changed"), only-same ("unchanged"), or "all"."""
    keep = _SELECT_KINDS.get(mode)
    changes = list(changes)
    return changes if keep is None else [c for c in changes if c.kind in keep]


def filter_class(changes: Iterable[ClassChange], needle: str) -> list[ClassChange]:
    """Keep changes whose lhs/rhs descriptor or source file contains `needle`
    (case-insensitive). Accepts a class name, package path, or descriptor."""
    n = needle.lower()
    out = []
    for c in changes:
        hay = " ".join(x for x in (c.lhs, c.rhs, c.source_file) if x).lower()
        if n in hay:
            out.append(c)
    return out


def sensitive_summary(changes: Iterable[ClassChange]) -> list[ClassChange]:
    """Paired classes that implement a sensitive component on both sides — the
    'both builds share a possibly-malicious implementation' signal."""
    from .components import SENSITIVE_CODES

    return [
        c for c in changes
        if c.lhs and c.rhs and (set(c.components) & SENSITIVE_CODES)
    ]


def render_components_text(changes: Iterable[ClassChange]) -> str:
    """Human headline block for the sensitive-component overlap (feedback #4)."""
    from .components import SENSITIVE_CODES, code_name

    changes = list(changes)
    shared = sensitive_summary(changes)
    if not shared:
        return "sensitive components: none shared across both builds"
    lines = [f"sensitive components shared by both builds ({len(shared)} classes):"]
    for c in sorted(shared, key=lambda x: (x.components, x.lhs or "")):
        codes = [f"{code}={code_name(code)}" for code in c.components if code in SENSITIVE_CODES]
        base = c.lhs_super or c.rhs_super or "?"
        lines.append(f"  [{','.join(codes)}] {c.lhs}  <->  {c.rhs}   (extends {base})")
    return "\n".join(lines)


_ORIGIN_TAG = {"app": "app", "library": "lib", "unknown": "?"}
_CONFIDENCE_RANK = {"high": 0, "low": 1}


def filter_min_confidence(changes: Iterable[ClassChange], min_confidence: str) -> list[ClassChange]:
    """Drop change verdicts below `min_confidence` ("high" | "low"). Only ever
    removes *low-confidence modified* rows (call-only churn in non-app code);
    added/removed/cosmetic/unchanged are always "high". `"low"` keeps everything."""
    floor = _CONFIDENCE_RANK.get(min_confidence, 1)
    return [c for c in changes if _CONFIDENCE_RANK.get(c.confidence, 0) <= floor]


def render_text(changes: Iterable[ClassChange], *, top: int = 30) -> str:
    """Human triage summary: counts (with an app-only breakdown), then the top
    modified/added/removed — ordered app-first, each tagged with its origin."""
    changes = list(changes)
    c = counts(changes)
    app = counts([x for x in changes if x.origin == "app"])
    any_app = any(x.origin == "app" for x in changes)
    hi_mod = sum(1 for x in changes if x.kind == "modified" and x.confidence == "high")
    lo_mod = sum(1 for x in changes if x.kind == "modified" and x.confidence == "low")
    lines = [
        "change summary: "
        + "  ".join(f"{k}={c.get(k, 0)}" for k in
                    ("modified", "added", "removed", "cosmetic", "unchanged")),
    ]
    shared_sensitive = sensitive_summary(changes)
    if shared_sensitive:
        codes = sorted({code for x in shared_sensitive for code in x.components})
        lines.append(
            f"  ⚠ sensitive components shared by both builds: "
            f"{len(shared_sensitive)} classes [{', '.join(codes)}] "
            f"(see the components section)"
        )
    if lo_mod:
        lines.append(
            f"  modified confidence: high={hi_mod}  low={lo_mod} "
            f"(low = call-only churn in non-app code, likely cross-toolchain noise)"
        )
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
        lines.append("\ntop modified (app-first, high-confidence first, then by magnitude):")
        for x in modified:
            src = x.source_file or x.lhs or "?"
            anc = " [anchored]" if x.anchored else ""
            lo = " [low-conf]" if x.confidence == "low" else ""
            lines.append(f"  {_tag(x)} [{x.magnitude:4d}] {src}  {x.summary}{anc}{lo}")
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
