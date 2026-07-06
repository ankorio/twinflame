"""Semantic feature layer — change-detection Phase 1.

The four-stage matcher answers *which* class in build A corresponds to which in
build B. It scores that correspondence with a structural similarity that also
moves on pure re-obfuscation / re-optimization noise, so the distance alone is a
poor *change* signal (a private benchmark app's v1->v2 run is the motivating case:
two builds with different R8 configs looked ~"all modified" structurally while most
of it was noise). This layer extracts the **obfuscation-robust semantic content**
whose *change between a matched pair* actually indicates behaviour change:

- **framework/library call targets** — R8 cannot rename `android/androidx/java/
  kotlin/...` descriptors, so the set of external APIs a class calls is stable
  across builds (modulo inlining relocating a call between classes; on
  release-vs-release both sides are optimised alike, so this is comparable).
- **string constants** — content survives renaming; a changed/added/removed
  string is a real edit (caveat: StringFog encryption defeats this — see
  plans/obfuscation-resilience-notes.md; a future flag drops this feature then).
- **framework type references** — superclass, interfaces, field types, and method
  prototype types that are framework-owned (thus name-stable).

**App-type references** become comparable once a **class-match map** (A-descriptor
-> B-descriptor, from the matcher) is threaded in: an app superclass/interface/
field/param type is renamed differently per build, but translating the LHS token
through the map lands it in B-naming so the two sides can be diffed. Only the
*matched universe* is compared (LHS refs whose owner the matcher paired, RHS refs
whose owner is a mapped target) — an untranslatable ref is left out rather than
reported as a spurious change (precision-biased; a genuinely new/removed class
still surfaces as an added/removed *class* at the Phase-2 level).

Deliberately *not* used yet (each needs loader/androguard work or a *method*-match
map): per-method field-access sets, basic-block shape, normalised numeric
constants, and *app*-call targets (translating `Lowner;->name(args)ret` needs the
method name mapped too, not just the owner class). Phase 2 (the change classifier
+ ranked report) consumes `class_change`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .anchor import FRAMEWORK_PREFIXES
from .model import Class, Method

# Class-match map: build-A class descriptor -> build-B class descriptor.
ClassMap = Mapping[str, str]


def _object_types(descriptor: str) -> list[str]:
    """Every `Lfoo/Bar;` token in a field/method type descriptor (arrays and
    multi-arg protos handled by scanning L...; spans)."""
    out: list[str] = []
    i, n = 0, len(descriptor)
    while i < n:
        if descriptor[i] == "L":
            end = descriptor.find(";", i)
            if end == -1:
                break
            out.append(descriptor[i : end + 1])
            i = end + 1
        else:
            i += 1
    return out


def _is_framework(desc: str) -> bool:
    return desc.startswith(FRAMEWORK_PREFIXES)


def _informative(s: str) -> bool:
    """Whether a string constant carries enough signal to count as a semantic
    delta. Drops empty / whitespace / punctuation-only / single-char strings
    (e.g. ``''``, ``' ['``, ``'"'``) that otherwise inflate the change magnitude
    without indicating a real edit — the noise seen on the private benchmark app. Same
    spirit as ``anchor.MIN_STRING_LEN``, but content- rather than length-based."""
    return sum(1 for ch in s if ch.isalnum()) >= 2


def framework_call_targets(c: Class) -> set[str]:
    """Distinct framework/library call targets across all of `c`'s methods.

    Full references (`Landroid/...;->m(...)ret`) — the most specific, fully
    rename-invariant behavioural fingerprint. Set (not multiset): a target
    appearing or disappearing is the change signal; call *counts* are noisier
    under optimisation.
    """
    return {
        ref for m in c.methods for ref in m.calls if _is_framework(ref)
    }


def _all_type_refs(c: Class) -> set[str]:
    """Every object-type descriptor `c` depends on: superclass, interfaces, field
    types, and method prototype types (framework and app alike, unfiltered)."""
    refs: set[str] = set()
    if c.superclass:
        refs.add(c.superclass)
    refs.update(c.interfaces)
    for f in c.fields:
        refs.update(_object_types(f.type_desc))
    for m in c.methods:
        refs.update(_object_types(m.descriptor))
    # A class never counts as depending on itself (self-references are not a
    # cross-class signal and would translate to identity noise).
    refs.discard(c.descriptor)
    return refs


def framework_type_refs(c: Class) -> set[str]:
    """Framework type descriptors `c` depends on — restricted to framework-owned
    (thus name-stable) descriptors, comparable across builds without a map."""
    return {t for t in _all_type_refs(c) if _is_framework(t)}


def app_type_refs(c: Class) -> set[str]:
    """*App* (non-framework) type descriptors `c` depends on. Names differ per
    build; only comparable after translation through a `ClassMap` (see
    `diff_features`)."""
    return {t for t in _all_type_refs(c) if not _is_framework(t)}


@dataclass(frozen=True, slots=True)
class ClassFeatures:
    """Obfuscation-robust semantic content of one class.

    `fw_calls`/`strings`/`fw_refs` are directly comparable across builds;
    `app_refs` are build-local descriptors, only comparable after translation
    through a `ClassMap` (owner class names differ per build)."""

    fw_calls: frozenset[str]
    strings: frozenset[str]
    fw_refs: frozenset[str]
    app_refs: frozenset[str]


def class_features(c: Class) -> ClassFeatures:
    return ClassFeatures(
        fw_calls=frozenset(framework_call_targets(c)),
        strings=frozenset(s for s in c.strings if _informative(s)),
        fw_refs=frozenset(framework_type_refs(c)),
        app_refs=frozenset(app_type_refs(c)),
    )


@dataclass(frozen=True, slots=True)
class FeatureDelta:
    """What changed, semantically, between two matched classes (A -> B).

    Each field is sorted for stable reporting. `is_semantic` is the gate the
    change classifier uses to separate real edits from cosmetic re-obfuscation
    noise; `magnitude` ranks review-worthiness.
    """

    calls_added: tuple[str, ...]
    calls_removed: tuple[str, ...]
    strings_added: tuple[str, ...]
    strings_removed: tuple[str, ...]
    refs_added: tuple[str, ...]
    refs_removed: tuple[str, ...]
    # App-type dependency deltas, in build-B naming (only populated when a
    # ClassMap was supplied to diff_features). Kept separate from the framework
    # `refs_*` so Phase 2 can weight app-logic changes vs. library churn.
    app_refs_added: tuple[str, ...] = ()
    app_refs_removed: tuple[str, ...] = ()

    @property
    def is_semantic(self) -> bool:
        return bool(
            self.calls_added or self.calls_removed
            or self.strings_added or self.strings_removed
            or self.refs_added or self.refs_removed
            or self.app_refs_added or self.app_refs_removed
        )

    @property
    def magnitude(self) -> int:
        return (
            len(self.calls_added) + len(self.calls_removed)
            + len(self.strings_added) + len(self.strings_removed)
            + len(self.refs_added) + len(self.refs_removed)
            + len(self.app_refs_added) + len(self.app_refs_removed)
        )

    def summary(self) -> str:
        """One-line human summary of the most reviewable deltas."""
        parts: list[str] = []
        if self.calls_added:
            parts.append(f"+{len(self.calls_added)} calls")
        if self.calls_removed:
            parts.append(f"-{len(self.calls_removed)} calls")
        if self.strings_added:
            parts.append(f"+{len(self.strings_added)} strings")
        if self.strings_removed:
            parts.append(f"-{len(self.strings_removed)} strings")
        if self.refs_added:
            parts.append(f"+{len(self.refs_added)} types")
        if self.refs_removed:
            parts.append(f"-{len(self.refs_removed)} types")
        if self.app_refs_added:
            parts.append(f"+{len(self.app_refs_added)} app-types")
        if self.app_refs_removed:
            parts.append(f"-{len(self.app_refs_removed)} app-types")
        return ", ".join(parts) if parts else "no semantic change"


def _sorted_diff(a: frozenset[str], b: frozenset[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(added = in b not a, removed = in a not b), both sorted."""
    return tuple(sorted(b - a)), tuple(sorted(a - b))


def _app_ref_diff(
    a: frozenset[str], b: frozenset[str], class_map: ClassMap
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Diff app-type dependencies within the *matched universe*.

    LHS refs are translated A->B through `class_map`; RHS refs are restricted to
    owners that are themselves mapped targets. Refs whose owner the matcher did
    not pair are excluded from both sides rather than reported as changes — an
    unmatched owner is a matcher gap (or a genuinely added/removed class, which
    Phase 2 reports at the class level), not evidence this class's dependency
    set changed.
    """
    lhs_translated = frozenset(class_map[t] for t in a if t in class_map)
    mapped_targets = frozenset(class_map.values())
    rhs_comparable = frozenset(t for t in b if t in mapped_targets)
    return _sorted_diff(lhs_translated, rhs_comparable)


def diff_features(
    a: ClassFeatures, b: ClassFeatures, class_map: Optional[ClassMap] = None
) -> FeatureDelta:
    calls_added, calls_removed = _sorted_diff(a.fw_calls, b.fw_calls)
    strings_added, strings_removed = _sorted_diff(a.strings, b.strings)
    refs_added, refs_removed = _sorted_diff(a.fw_refs, b.fw_refs)
    if class_map is not None:
        app_refs_added, app_refs_removed = _app_ref_diff(a.app_refs, b.app_refs, class_map)
    else:
        app_refs_added, app_refs_removed = (), ()
    return FeatureDelta(
        calls_added=calls_added, calls_removed=calls_removed,
        strings_added=strings_added, strings_removed=strings_removed,
        refs_added=refs_added, refs_removed=refs_removed,
        app_refs_added=app_refs_added, app_refs_removed=app_refs_removed,
    )


def class_change(
    lhs: Class, rhs: Class, class_map: Optional[ClassMap] = None
) -> FeatureDelta:
    """Semantic delta between a matched class pair (lhs = build A, rhs = build B).

    When `class_map` is given, app-type dependency changes are included (their
    owner names translated A->B); without it only framework-stable features are
    compared (backward-compatible)."""
    return diff_features(class_features(lhs), class_features(rhs), class_map)


# --- per-method localization ------------------------------------------------

def method_framework_calls(m: Method) -> set[str]:
    """Framework/library call targets this single method invokes — the
    rename-invariant, method-local behavioural signal used to localise a class
    change down to the method(s) that actually changed behaviour."""
    return {ref for ref in m.calls if _is_framework(ref)}


@dataclass(frozen=True, slots=True)
class MethodDelta:
    """A localized method-level change inside a modified class.

    `name`/`descriptor` are obfuscated (locate the method via the deobfuscation
    map + decompiler); the actionable content is the status and the
    framework-call delta / size.
    """

    status: str                 # "modified" | "added" | "deleted"
    name: str                   # obfuscated method name
    descriptor: str             # method proto, e.g. "(Landroid/os/Bundle;)V"
    instr_count: int            # method size (new size for added/modified)
    calls_added: tuple[str, ...]
    calls_removed: tuple[str, ...]

    @property
    def call_delta(self) -> int:
        return len(self.calls_added) + len(self.calls_removed)

    def summary(self) -> str:
        if self.status == "added":
            return f"added method ({self.instr_count} instr)"
        if self.status == "deleted":
            return f"removed method ({self.instr_count} instr)"
        parts = []
        if self.calls_added:
            parts.append(f"+{len(self.calls_added)} calls")
        if self.calls_removed:
            parts.append(f"-{len(self.calls_removed)} calls")
        return "method " + (", ".join(parts) if parts else "body changed")


# Localization order: behavioural edits first (largest call-delta), then new
# methods, then removed, each by size — most-review-worthy first within a class.
_METHOD_STATUS_RANK = {"modified": 0, "added": 1, "deleted": 2}


def localize_method_changes(method_matches) -> tuple[MethodDelta, ...]:
    """Turn a match's per-method verdicts into localized `MethodDelta`s.

    `modified` methods are surfaced **only when their framework-call set moved**
    (a body-only re-optimization with no external-call change is indistinguishable
    from cosmetic noise given the features we carry, so it is not localized).
    `added`/`deleted` methods are always structural and always surfaced.
    """
    out: list[MethodDelta] = []
    for mm in method_matches:
        if mm.status == "added" and mm.rhs is not None:
            out.append(MethodDelta("added", mm.rhs.name, mm.rhs.descriptor,
                                   mm.rhs.instr_count,
                                   tuple(sorted(method_framework_calls(mm.rhs))), ()))
        elif mm.status == "deleted" and mm.lhs is not None:
            out.append(MethodDelta("deleted", mm.lhs.name, mm.lhs.descriptor,
                                   mm.lhs.instr_count,
                                   (), tuple(sorted(method_framework_calls(mm.lhs)))))
        elif mm.status == "modified" and mm.lhs is not None and mm.rhs is not None:
            added, removed = _sorted_diff(
                frozenset(method_framework_calls(mm.lhs)),
                frozenset(method_framework_calls(mm.rhs)),
            )
            if added or removed:  # only localize a behavioural (call-set) change
                out.append(MethodDelta("modified", mm.rhs.name, mm.rhs.descriptor,
                                       mm.rhs.instr_count, added, removed))
    out.sort(key=lambda d: (_METHOD_STATUS_RANK.get(d.status, 9),
                            -d.call_delta, -d.instr_count, d.name))
    return tuple(out)
