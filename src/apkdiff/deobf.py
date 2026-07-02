"""Cross-version deobfuscation mapping (plan H4).

Given a diff between a *donor* build (apk1 / lhs — retains useful names, e.g.
the DEX `SourceFile` attribute) and a *target* build (apk2 / rhs — obfuscated,
`SourceFile` stripped), propagate the recovered class names from each matched
donor class onto the target's obfuscated class and emit a ProGuard-format
`mapping.txt`.

JADX/Ghidra/retrace consume that mapping and propagate the rename to *every*
reference automatically — so we never rewrite the DEX ourselves.

What this recovers and what it doesn't:
- Recovers the class **simple name** (from the donor's source file, e.g.
  `ContextCompat.java` -> `ContextCompat`), or the donor's own clear class name
  when it isn't obfuscated.
- Keeps the target's (obfuscated) **package** and **inner-class structure** —
  source files carry no package, so `x6.q$a` becomes `x6.ContextCompat$a`.
- Does **not** recover method/field names here (a documented next step); a
  source file only names the class.

Confidence: anchored matches and exact (distance == 1.0) matches are trusted;
others are included only at/above `min_confidence` and flagged with a comment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Class, Match

# Source extensions whose stem is the class simple-name.
_CODE_EXT = (".java", ".kt", ".scala", ".groovy")
# Anything that isn't a legal Java identifier char.
_NON_IDENT = re.compile(r"[^A-Za-z0-9_$]")
# Below this similarity a non-anchored match is annotated as low-confidence.
_TRUSTED = 0.95


@dataclass(frozen=True)
class MappingEntry:
    original: str      # recovered, human-readable FQCN (left side)
    obfuscated: str    # name present in the target APK (right side)
    confidence: float
    anchored: bool


def _looks_obfuscated(simple: str) -> bool:
    """Heuristic: a renamed leaf is very short, or a short all-lowercase token."""
    if not simple:
        return True
    if len(simple) <= 2:
        return True
    return simple.islower() and len(simple) <= 3


def _sanitize(name: str) -> str:
    name = _NON_IDENT.sub("_", name)
    if name and name[0].isdigit():
        name = "_" + name
    return name


def recovered_head(donor: Class) -> str | None:
    """Best human-readable simple name for a donor class, or None.

    Prefers the donor's source-file stem; falls back to the donor's own simple
    name when that isn't itself obfuscated.
    """
    sf = donor.source_file
    if sf and sf != "SourceFile":
        stem = sf.replace("\\", "/").rsplit("/", 1)[-1]
        low = stem.lower()
        for ext in _CODE_EXT:
            if low.endswith(ext):
                stem = stem[: -len(ext)]
                break
        else:
            # No known code extension (e.g. a stray "Foo.2"): keep the head.
            if "." in stem:
                stem = stem.split(".", 1)[0]
        stem = _sanitize(stem)
        if stem and not _looks_obfuscated(stem):
            return stem

    base = _sanitize(donor.name.split("$")[0])
    if base and not _looks_obfuscated(base):
        return base
    return None


def _fqcn(pkg: str, name: str) -> str:
    return f"{pkg}.{name}" if pkg else name


def _original_fqcn(target: Class, head: str) -> str:
    """Swap the target's obfuscated outer leaf for `head`, keep package+inners."""
    parts = target.name.split("$")
    parts[0] = head
    return _fqcn(target.package, "$".join(parts))


def build_mapping(matches: list[Match], *, min_confidence: float = 0.8) -> list[MappingEntry]:
    """Build deobfuscation entries for the rhs (target) side of each match.

    lhs is the donor (names recovered from it); rhs is the obfuscated target
    the mapping is written for.
    """
    # Collect candidates, highest-confidence first so the cleanest names win the
    # un-suffixed slot on collision.
    candidates: list[tuple[Class, str, float, bool]] = []
    for m in matches:
        if not m.is_paired or m.lhs is None or m.rhs is None:
            continue
        target_leaf = m.rhs.name.split("$")[0]
        if not _looks_obfuscated(target_leaf):
            continue  # target already carries a real name; don't clobber it
        anchored = m.breakdown.get("anchored") == 1.0
        if not (anchored or m.distance >= min_confidence):
            continue
        head = recovered_head(m.lhs)
        if head is None:
            continue
        candidates.append((m.rhs, head, m.distance, anchored))

    candidates.sort(key=lambda c: (-(c[3]), -c[2]))  # anchored, then distance

    used: dict[str, str] = {}  # original_fqcn -> obfuscated_fqcn that claimed it
    entries: list[MappingEntry] = []
    for target, head, conf, anchored in candidates:
        obf = _fqcn(target.package, target.name)
        original = _original_fqcn(target, head)
        if used.get(original, obf) != obf:
            # Collision with a different class: disambiguate with the obf leaf.
            original = _original_fqcn(target, f"{head}_{target.name.split('$')[0]}")
        used[original] = obf
        entries.append(MappingEntry(original, obf, conf, anchored))

    entries.sort(key=lambda e: e.obfuscated)
    return entries


def render_mapping(entries: list[MappingEntry]) -> str:
    """Render entries as a ProGuard-format mapping.txt (class lines only)."""
    out: list[str] = [
        "# apkdiff cross-version deobfuscation map",
        "# format: <recovered name> -> <obfuscated name in target APK>:",
        "# package stays obfuscated (source files carry no package); only the",
        "# class simple name is recovered. Low-confidence lines are flagged.",
        "#",
        "# JADX: jadx --mappings-path THIS_FILE -Prename-mappings.format=PROGUARD_FILE \\",
        "#            -Prename-mappings.invert=yes -d OUT_DIR TARGET_APK",
        "# (invert=yes is required — jadx's PROGUARD_FILE reader expects the",
        "# opposite direction from standard ProGuard mapping.txt / retrace).",
    ]
    for e in entries:
        if not e.anchored and e.confidence < _TRUSTED:
            out.append(f"# low-confidence ({e.confidence:.2f})")
        out.append(f"{e.original} -> {e.obfuscated}:")
    return "\n".join(out) + "\n"
