"""Synthetic mutation harness — a ground-truth oracle for the change classifier.

The release-vs-release corpora tell us the *matcher* is accurate, but there is no
oracle for "did the change classifier flag exactly what changed?" — the verdicts
(modified / cosmetic / added / removed) and their localized deltas were only ever
spot-checked on the private benchmark app. This builds a controllable one: take a
baseline synthetic app, apply *labeled* mutations, and the test asserts the full
`api.diff -> changes.change_set` pipeline reports exactly those changes and nothing
else.

Design choices that keep the oracle about the *classifier*, not matching luck:
- Every baseline class carries a unique anchor string, so the pair is locked in by
  `anchor.py` regardless of how far the body drifts — pairing is guaranteed, so a
  misclassification can't be blamed on a missed match.
- Persistent classes keep their descriptor (no rename); rename-robust matching is
  covered by the release-vs-release corpora, not here.
- Mutations touch one obfuscation-robust feature each (framework call, string,
  method add/delete, or pure bytecode churn) so the expected delta is unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from apkdiff.model import AccessFlag, Class, Method

import synthetic as syn  # fixtures dir is on sys.path when tests import this

LOG_D = "Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I"
URI_PARSE = "Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"
BUNDLE_INIT = "Landroid/os/Bundle;-><init>()V"
TOAST = "Landroid/widget/Toast;->show()V"


# --- mutation primitives (each returns a new frozen Class) -------------------

def add_call(cls: Class, method_name: str, call: str) -> Class:
    methods = tuple(
        replace(m, calls=m.calls + (call,)) if m.name == method_name else m
        for m in cls.methods
    )
    return replace(cls, methods=methods)


def remove_call(cls: Class, method_name: str, call: str) -> Class:
    methods = tuple(
        replace(m, calls=tuple(c for c in m.calls if c != call)) if m.name == method_name else m
        for m in cls.methods
    )
    return replace(cls, methods=methods)


def add_string(cls: Class, s: str) -> Class:
    return replace(cls, strings=cls.strings + (s,))


def add_method(cls: Class, method: Method) -> Class:
    return replace(cls, methods=cls.methods + (method,))


def delete_method(cls: Class, method_name: str) -> Class:
    return replace(cls, methods=tuple(m for m in cls.methods if m.name != method_name))


def churn_bytecode(cls: Class, method_name: str) -> Class:
    """Perturb a couple of a method's opcodes *in place* (same length) — enough to
    move structural distance below 1.0 (so the class isn't "unchanged") but mild
    enough that the method still pairs (>= the 0.4 method floor) and every semantic
    feature (calls/strings/types) is untouched → the class is *cosmetic*.

    Flips to opcodes in clearly different categories (invoke, arithmetic) so the
    abstract-opcode stream actually changes, not just the raw bytes."""
    def upd(m: Method) -> Method:
        ba = bytearray(m.bytecode)
        for i, op in ((0, 0x6E), (1, 0x90)):  # invoke-virtual, add-int categories
            if i < len(ba) - 1:               # keep the trailing return opcode
                ba[i] = op
        nb = bytes(ba)
        xor = 0
        for b in nb:
            xor ^= b
        return replace(m, bytecode=nb, instr_count=len(nb), opcode_xor=xor)
    methods = tuple(upd(m) if m.name == method_name else m for m in cls.methods)
    return replace(cls, methods=methods)


def _by_descriptor(classes: list[Class]) -> dict[str, Class]:
    return {c.descriptor: c for c in classes}


# --- expected-verdict record ------------------------------------------------

@dataclass(frozen=True)
class ExpectedClass:
    descriptor: str
    kind: str
    calls_added: frozenset[str] = frozenset()
    calls_removed: frozenset[str] = frozenset()
    strings_added: frozenset[str] = frozenset()
    strings_removed: frozenset[str] = frozenset()
    methods_added: frozenset[str] = frozenset()      # method names
    methods_deleted: frozenset[str] = frozenset()    # method names


# --- the baseline app -------------------------------------------------------

def _method(name, calls=(), instr=4, desc="()V", ret="V", args=0):
    # distinct bytecode per name so classes have real structure
    bc = bytes([0x12 + (sum(ord(ch) for ch in name) % 6)] * (instr - 1) + [0x0E])
    return syn.MethodSpec(name=name, descriptor=desc, calls=calls, instr_count=instr,
                          bytecode=bc, return_type=ret, arg_count=args)


def baseline_app(package: str = "com.acme.app") -> list[Class]:
    base = "L" + package.replace(".", "/")
    specs = [
        # C0 — will gain a framework call
        syn.ClassSpec(descriptor=f"{base}/C0;", source_file="C0.java",
                      strings=("anchor_c0_alpha",),
                      methods=(_method("<init>"), _method("run", calls=(LOG_D,)))),
        # C1 — will lose a framework call
        syn.ClassSpec(descriptor=f"{base}/C1;", source_file="C1.java",
                      strings=("anchor_c1_bravo",),
                      methods=(_method("<init>"), _method("run", calls=(LOG_D, URI_PARSE)))),
        # C2 — will gain a string
        syn.ClassSpec(descriptor=f"{base}/C2;", source_file="C2.java",
                      strings=("anchor_c2_charlie",),
                      methods=(_method("<init>"), _method("load", calls=(BUNDLE_INIT,)))),
        # C3 — will gain a method (with a framework call)
        syn.ClassSpec(descriptor=f"{base}/C3;", source_file="C3.java",
                      strings=("anchor_c3_delta",),
                      methods=(_method("<init>"), _method("op", calls=(LOG_D,)))),
        # C4 — will lose a method that has NO framework call (structural only)
        syn.ClassSpec(descriptor=f"{base}/C4;", source_file="C4.java",
                      strings=("anchor_c4_echo",),
                      methods=(_method("<init>"), _method("compute", instr=6),
                               _method("helper", instr=5))),
        # C5 — bytecode churn only → cosmetic
        syn.ClassSpec(descriptor=f"{base}/C5;", source_file="C5.java",
                      strings=("anchor_c5_foxtrot",),
                      methods=(_method("<init>"), _method("work", calls=(TOAST,), instr=8))),
        # C6 — untouched → unchanged
        syn.ClassSpec(descriptor=f"{base}/C6;", source_file="C6.java",
                      strings=("anchor_c6_golf",),
                      methods=(_method("<init>"), _method("noop", instr=5))),
        # C7 — removed entirely
        syn.ClassSpec(descriptor=f"{base}/C7;", source_file="C7.java",
                      strings=("anchor_c7_hotel",),
                      methods=(_method("<init>"), _method("gone", calls=(LOG_D,), instr=7))),
    ]
    return syn.make_classes(specs)


def mutation_scenario(package: str = "com.acme.app"):
    """Return (baseline, mutated, expected_by_descriptor).

    `expected_by_descriptor` is keyed by the descriptor the change is reported
    under (lhs for modified/cosmetic/removed, rhs for added)."""
    base = "L" + package.replace(".", "/")
    baseline = baseline_app(package)
    idx = _by_descriptor(baseline)

    mutated_map = dict(idx)  # start from a copy; edit in place

    mutated_map[f"{base}/C0;"] = add_call(idx[f"{base}/C0;"], "run", TOAST)
    mutated_map[f"{base}/C1;"] = remove_call(idx[f"{base}/C1;"], "run", URI_PARSE)
    mutated_map[f"{base}/C2;"] = add_string(idx[f"{base}/C2;"], "brand_new_feature_flag")
    new_m = syn.make_method(_method("added", calls=(URI_PARSE,), instr=6))
    mutated_map[f"{base}/C3;"] = add_method(idx[f"{base}/C3;"], new_m)
    mutated_map[f"{base}/C4;"] = delete_method(idx[f"{base}/C4;"], "helper")
    mutated_map[f"{base}/C5;"] = churn_bytecode(idx[f"{base}/C5;"], "work")
    # C6 untouched. C7 removed:
    del mutated_map[f"{base}/C7;"]
    # Brand-new class in the newer build. Made structurally distinct (5 methods +
    # fields) from the removed C7 (2 methods) so the structural stage cannot
    # force-pair them — C7 must stay "removed" and C8 "added".
    added_cls = syn.make_class(syn.ClassSpec(
        descriptor=f"{base}/C8;", source_file="C8.java", strings=("anchor_c8_new",),
        fields=(syn.FieldSpec(name="a", type_desc="I"),
                syn.FieldSpec(name="b", type_desc="Ljava/lang/String;")),
        methods=(_method("<init>"), _method("m1", instr=3), _method("m2", instr=4, args=1),
                 _method("m3", calls=(BUNDLE_INIT,), instr=5),
                 _method("brandnew", calls=(LOG_D, TOAST), instr=9))))
    mutated_map[f"{base}/C8;"] = added_cls

    mutated = list(mutated_map.values())

    expected = {
        f"{base}/C0;": ExpectedClass(f"{base}/C0;", "modified", calls_added=frozenset({TOAST})),
        f"{base}/C1;": ExpectedClass(f"{base}/C1;", "modified", calls_removed=frozenset({URI_PARSE})),
        f"{base}/C2;": ExpectedClass(f"{base}/C2;", "modified", strings_added=frozenset({"brand_new_feature_flag"})),
        f"{base}/C3;": ExpectedClass(f"{base}/C3;", "modified", calls_added=frozenset({URI_PARSE}),
                                     methods_added=frozenset({"added"})),
        f"{base}/C4;": ExpectedClass(f"{base}/C4;", "modified", methods_deleted=frozenset({"helper"})),
        f"{base}/C5;": ExpectedClass(f"{base}/C5;", "cosmetic"),
        f"{base}/C6;": ExpectedClass(f"{base}/C6;", "unchanged"),
        f"{base}/C7;": ExpectedClass(f"{base}/C7;", "removed"),
        f"{base}/C8;": ExpectedClass(f"{base}/C8;", "added"),
    }
    return baseline, mutated, expected
