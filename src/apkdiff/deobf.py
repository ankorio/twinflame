"""Cross-version deobfuscation mapping (plan H4, M2.3).

Given a diff between a *donor* build (apk1 / lhs — retains useful names, e.g.
the DEX `SourceFile` attribute) and a *target* build (apk2 / rhs — obfuscated,
`SourceFile` stripped), propagate the recovered class, method, and field names
from each matched donor class onto the target's obfuscated class and emit a
ProGuard-format `mapping.txt`.

JADX/Ghidra/retrace consume that mapping and propagate the rename to *every*
reference automatically — so we never rewrite the DEX ourselves.

What this recovers and what it doesn't:
- Recovers the class **simple name** (from the donor's source file, e.g.
  `ContextCompat.java` -> `ContextCompat`), or the donor's own clear class name
  when it isn't obfuscated.
- Keeps the target's (obfuscated) **package** and **inner-class structure** —
  source files carry no package, so `x6.q$a` becomes `x6.ContextCompat$a`.
- Recovers **method names** for every method M1.1 paired within a matched
  class (`matched` unconditionally, `modified` above `min_confidence`) —
  method member lines carry the donor's own Java-style signature so overload
  resolution stays unambiguous.
- Recovers **field names** conservatively: fields have no bytecode to diff, so
  a pair is only trusted when donor and target agree on both declared
  position *and* type descriptor — no fuzzy scoring, higher bar than methods.

Confidence: anchored matches and exact (distance == 1.0) matches are trusted;
others are included only at/above `min_confidence` and flagged with a comment.

Known limitation (found via real-APK verification against jadx): a method
member line's signature uses the *donor's* type names for its params/return
(e.g. `void b(com.acme.Helper) -> g`). A real R8 `mapping.txt` is
self-consistent — every referenced type has its own class-level entry in the
*same* file, so the consuming tool can resolve a param type back to its
target-side descriptor. Ours only has entries for classes we independently
matched with enough confidence, so a method whose parameter/return type is
some *other* app class we didn't also successfully rename produces a
signature jadx can't resolve to a real target method — it silently skips
that one rename rather than erroring. Framework types (`java.*`, `android.*`)
are never obfuscated, so those signatures always resolve fine; this only
bites app-to-app type references. Not fixed here — would need a second,
cross-referencing pass over the whole mapping, out of scope for the current
"smallest useful version".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Class, Field, Match, Method, MethodMatch

# Source extensions whose stem is the class simple-name.
_CODE_EXT = (".java", ".kt", ".scala", ".groovy")
# Anything that isn't a legal Java identifier char.
_NON_IDENT = re.compile(r"[^A-Za-z0-9_$]")
# Below this similarity a non-anchored match is annotated as low-confidence.
_TRUSTED = 0.95

_PRIMITIVE_JAVA = {
    "V": "void",
    "Z": "boolean",
    "B": "byte",
    "S": "short",
    "C": "char",
    "I": "int",
    "J": "long",
    "F": "float",
    "D": "double",
}


@dataclass(frozen=True)
class MemberEntry:
    """One method or field rename inside a class block."""

    original_signature: str  # donor-side Java signature, e.g. "int getValue()"
    original_name: str
    obfuscated_name: str
    confidence: float
    trusted: bool  # True => no "# low-confidence" comment needed


@dataclass(frozen=True)
class MappingEntry:
    original: str      # recovered, human-readable FQCN (left side)
    obfuscated: str    # name present in the target APK (right side)
    confidence: float
    anchored: bool
    methods: tuple[MemberEntry, ...] = ()
    fields: tuple[MemberEntry, ...] = ()


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


def _java_type_name(descriptor: str) -> str:
    """JVM/dex type descriptor -> Java source type name.

    "I" -> "int", "[I" -> "int[]", "Lfoo/bar/Baz;" -> "foo.bar.Baz".
    """
    depth = 0
    i = 0
    while i < len(descriptor) and descriptor[i] == "[":
        depth += 1
        i += 1
    body = descriptor[i:]
    if body.startswith("L") and body.endswith(";"):
        base = body[1:-1].replace("/", ".")
    else:
        base = _PRIMITIVE_JAVA.get(body, body)
    return base + "[]" * depth


def _split_method_descriptor(descriptor: str) -> tuple[list[str], str]:
    """Parse a dex method descriptor "(args)ret" into (arg descriptors, return descriptor).

    androguard's `Method.descriptor` separates multi-arg protos with spaces
    (its own "pretty" format, e.g. "(Ljava/lang/String; I)I", not the raw
    compact JVM descriptor) — strip them so they aren't parsed as their own
    zero-width "argument" (see the matching fix in loader.py::_count_params).
    """
    params_str, _, ret = descriptor[1:].partition(")")
    params_str = params_str.replace(" ", "")
    args: list[str] = []
    i = 0
    while i < len(params_str):
        start = i
        while params_str[i] == "[":
            i += 1
        if params_str[i] == "L":
            i = params_str.index(";", i) + 1
        else:
            i += 1
        args.append(params_str[start:i])
    return args, ret.strip()


def _method_java_signature(m: Method) -> str:
    """Donor-side "returnType name(argType,argType)" for a mapping.txt method line.

    No space after the comma: real ProGuard mapping.txt (and the parser jadx/
    retrace use) treats the member line as column-separated by whitespace, so
    a space inside the parens is misread as an extra column.
    """
    args, ret = _split_method_descriptor(m.descriptor)
    arg_types = ",".join(_java_type_name(a) for a in args)
    return f"{_java_type_name(ret)} {m.name}({arg_types})"


def _field_java_signature(f: Field) -> str:
    return f"{_java_type_name(f.type_desc)} {f.name}"


def _method_members(
    method_matches: tuple[MethodMatch, ...], *, min_confidence: float
) -> list[MemberEntry]:
    """Method member entries (M1.1 verdicts -> mapping.txt member lines).

    `matched` (identical, score 1.0) is unconditional; `modified` is included
    only at/above `min_confidence`, same floor as the class-level entries.
    `added`/`deleted` have no donor<->target pair to name, so they're skipped.
    """
    out: list[MemberEntry] = []
    for mm in method_matches:
        if mm.status not in ("matched", "modified") or mm.lhs is None or mm.rhs is None:
            continue
        if mm.status == "modified" and mm.score < min_confidence:
            continue
        out.append(
            MemberEntry(
                original_signature=_method_java_signature(mm.lhs),
                original_name=mm.lhs.name,
                obfuscated_name=mm.rhs.name,
                confidence=mm.score,
                trusted=mm.score >= _TRUSTED,
            )
        )
    return out


def _field_members(lhs_fields: tuple[Field, ...], rhs_fields: tuple[Field, ...]) -> list[MemberEntry]:
    """Best-effort positional field pairing: same declared index *and* type.

    Fields carry no bytecode to diff, so this is a much weaker signal than
    method matching (which scores actual opcode similarity) — only pair when
    both position and type agree, and leave everything else unmapped rather
    than guess. Always "trusted": the strict criteria are the confidence bar.
    """
    out: list[MemberEntry] = []
    for lf, rf in zip(lhs_fields, rhs_fields):
        if lf.type_desc != rf.type_desc:
            continue
        out.append(
            MemberEntry(
                original_signature=_field_java_signature(lf),
                original_name=lf.name,
                obfuscated_name=rf.name,
                confidence=1.0,
                trusted=True,
            )
        )
    return out


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
    candidates: list[tuple[Match, str, float, bool]] = []
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
        candidates.append((m, head, m.distance, anchored))

    candidates.sort(key=lambda c: (-(c[3]), -c[2]))  # anchored, then distance

    used: dict[str, str] = {}  # original_fqcn -> obfuscated_fqcn that claimed it
    entries: list[MappingEntry] = []
    for match, head, conf, anchored in candidates:
        target = match.rhs
        obf = _fqcn(target.package, target.name)
        original = _original_fqcn(target, head)
        if used.get(original, obf) != obf:
            # Collision with a different class: disambiguate with the obf leaf.
            original = _original_fqcn(target, f"{head}_{target.name.split('$')[0]}")
        used[original] = obf
        methods = _method_members(match.method_matches, min_confidence=min_confidence)
        fields = _field_members(match.lhs.fields, match.rhs.fields)
        entries.append(MappingEntry(original, obf, conf, anchored, tuple(methods), tuple(fields)))

    entries.sort(key=lambda e: e.obfuscated)
    return entries


def render_mapping(entries: list[MappingEntry]) -> str:
    """Render entries as a ProGuard-format mapping.txt.

    Class lines carry indented member lines underneath for every paired
    method (M1.1 verdicts) and conservatively-paired field (M2.3) — the same
    `# low-confidence` convention as class lines, applied per member.
    """
    out: list[str] = [
        "# apkdiff cross-version deobfuscation map",
        "# format: <recovered name> -> <obfuscated name in target APK>:",
        "#         indented member lines rename methods/fields within a class",
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
        for f in sorted(e.fields, key=lambda x: x.obfuscated_name):
            if not f.trusted:
                out.append(f"    # low-confidence ({f.confidence:.2f})")
            out.append(f"    {f.original_signature} -> {f.obfuscated_name}")
        for meth in sorted(e.methods, key=lambda x: x.obfuscated_name):
            if not meth.trusted:
                out.append(f"    # low-confidence ({meth.confidence:.2f})")
            out.append(f"    {meth.original_signature} -> {meth.obfuscated_name}")
    return "\n".join(out) + "\n"
