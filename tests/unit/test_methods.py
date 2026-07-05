"""M1.1 — method-level matching."""

from __future__ import annotations

import synthetic

from apkdiff.accurate import compare_classes, match_methods, method_similarity


def _m(name, bytecode, *, ret="V", args=0, instr=None, xref=0):
    return synthetic.make_method(
        synthetic.MethodSpec(
            name=name,
            return_type=ret,
            arg_count=args,
            bytecode=bytecode,
            instr_count=instr if instr is not None else len(bytecode),
            xref_count=xref,
        )
    )


def test_method_similarity_identical_is_one():
    m = _m("a", bytes([0x12, 0x6E, 0x0E]))
    assert method_similarity(m, m) == 1.0


def test_method_similarity_body_change_drops_below_one():
    a = _m("a", bytes([0x12, 0x6E, 0x0E]))
    b = _m("a", bytes([0x12, 0x90, 0x91, 0x6E, 0x0E]))
    s = method_similarity(a, b)
    assert 0.0 <= s < 1.0


def test_method_similarity_proto_mismatch_penalized():
    a = _m("a", bytes([0x12, 0x0F]), ret="I")
    b = _m("a", bytes([0x12, 0x0F]), ret="Ljava/lang/String;", args=2)
    # Same bytecode, different return type and arg count → below 1.0.
    assert method_similarity(a, b) < 1.0


def test_match_methods_pairs_by_body_not_position():
    a_methods = (_m("x", bytes([0x12, 0x6E, 0x0E])), _m("y", bytes([0x90, 0x91, 0x0E])))
    # Same two methods, declared in the opposite order.
    b_methods = tuple(reversed(a_methods))
    mms = match_methods(a_methods, b_methods)
    assert all(mm.status == "matched" for mm in mms)
    assert len(mms) == 2


def test_match_methods_reports_added_and_deleted():
    # "keep" is identical; "gone" and "new" differ in both prototype and body,
    # so they read as a deletion + an addition rather than a modification.
    a_methods = (
        _m("keep", bytes([0x12, 0x0E])),
        _m("gone", bytes([0x90, 0x91, 0x92, 0x93, 0x94, 0x0F]), ret="I"),
    )
    b_methods = (
        _m("keep", bytes([0x12, 0x0E])),
        _m("new", bytes([0x44, 0x45, 0x46, 0x47, 0x48, 0x0E]), ret="V", args=2),
    )
    statuses = sorted(mm.status for mm in match_methods(a_methods, b_methods))
    assert statuses == ["added", "deleted", "matched"]


def test_compare_classes_flags_single_modified_method():
    base = synthetic.make_class(
        synthetic.ClassSpec(descriptor="LA;", methods=synthetic.standard_methods())
    )
    mutated = synthetic.mutate_method_bytecode(
        base, "getValue", bytes([0x12, 0x12, 0x90, 0x91, 0x6E, 0x0F])
    )
    dist, breakdown, mms = compare_classes(base, mutated)
    assert dist < 1.0
    modified = [mm for mm in mms if mm.status == "modified"]
    assert [mm.name for mm in modified] == ["getValue"]
    assert breakdown["methods_modified"] == 1.0
    # Everything else is a clean match.
    assert sum(1 for mm in mms if mm.status == "matched") == len(mms) - 1


def test_compare_classes_self_is_perfect():
    c = synthetic.make_class(
        synthetic.ClassSpec(
            descriptor="LA;",
            methods=synthetic.standard_methods(),
            fields=synthetic.standard_fields(),
        )
    )
    dist, breakdown, mms = compare_classes(c, c)
    assert dist == 1.0
    assert all(mm.status == "matched" for mm in mms)
    assert breakdown["bytecode"] == 1.0
