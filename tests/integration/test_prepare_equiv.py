"""Prepare/compare split — the record must be lossless for comparison.

A diff run off a `PreparedRecord`'s classes (bytecode dropped, signature +
abstract-opcode sequence cached) must be identical to a diff off a fresh parse,
both in-memory and after a JSON serialize/deserialize round-trip. This is the
invariant that lets the pipeline persist per-sample records and reuse them.
"""

from __future__ import annotations

from pathlib import Path

import synthetic

from twinflame import api
from twinflame.model import App
from twinflame.prepare import (
    prepare_app,
    record_from_bytes,
    record_from_dict,
    record_to_bytes,
    record_to_dict,
)


def _summary(matches):
    """Canonical, order-independent view of a diff result."""
    out = []
    for m in matches:
        out.append((
            m.lhs.descriptor if m.lhs else None,
            m.rhs.descriptor if m.rhs else None,
            round(m.distance, 9),
            tuple(sorted((m.breakdown or {}).items())),
            tuple((mm.name, mm.status, round(mm.score, 9)) for mm in m.method_matches),
        ))
    return sorted(out, key=lambda t: (t[0] or "", t[1] or "", t[2]))


def _record(classes, digest):
    return prepare_app(App(Path(digest), tuple(classes), None),
                       digest=digest, input_kind="apk")


def _build_pair():
    lhs = synthetic.vendor_app(n_classes=8)
    rhs = list(lhs)
    rhs[3] = synthetic.mutate_method_bytecode(
        rhs[3], method_name="op0",
        new_bytecode=bytes([0x12, 0x12, 0x90, 0x91, 0x6E, 0x6E, 0x0F]),
    )
    return lhs, rhs


def test_prepared_class_drops_bytecode_keeps_abstract():
    rec = _record(synthetic.vendor_app(n_classes=3), "d")
    for c in rec.classes:
        assert c.signature is not None
        for m in c.methods:
            assert m.bytecode == b""
            assert m.abstract is not None


def test_diff_off_record_matches_fresh_parse():
    lhs, rhs = _build_pair()
    fresh = api.diff(lhs, rhs, threshold=0.6)

    rec_l, rec_r = _record(lhs, "l"), _record(rhs, "r")
    prepared = api.diff(list(rec_l.classes), list(rec_r.classes), threshold=0.6)

    assert _summary(prepared) == _summary(fresh)


def test_diff_off_serialized_record_matches_fresh_parse():
    lhs, rhs = _build_pair()
    fresh = api.diff(lhs, rhs, threshold=0.6)

    rec_l = record_from_dict(record_to_dict(_record(lhs, "l")))
    rec_r = record_from_dict(record_to_dict(_record(rhs, "r")))
    prepared = api.diff(list(rec_l.classes), list(rec_r.classes), threshold=0.6)

    assert _summary(prepared) == _summary(fresh)


def test_diff_off_packed_record_matches_fresh_parse():
    lhs, rhs = _build_pair()
    fresh = api.diff(lhs, rhs, threshold=0.6)

    rec_l = record_from_bytes(record_to_bytes(_record(lhs, "l")))
    rec_r = record_from_bytes(record_to_bytes(_record(rhs, "r")))
    prepared = api.diff(list(rec_l.classes), list(rec_r.classes), threshold=0.6)

    assert _summary(prepared) == _summary(fresh)


def test_signatures_identical_fresh_vs_prepared():
    from twinflame.signature import compute_signature

    classes = synthetic.vendor_app(n_classes=5)
    rec = _record(classes, "d")
    for fresh_c, prep_c in zip(classes, rec.classes):
        assert compute_signature(fresh_c) == compute_signature(prep_c)
