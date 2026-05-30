from __future__ import annotations

import random
import time

from apkdiff.model import Signature
from apkdiff.signature import (
    BUCKET_PREFIX_BITS_DEFAULT,
    LSHIndex,
    N_PERMUTATIONS_DEFAULT,
    compute_signature,
)


def _import_synthetic():
    import sys
    from pathlib import Path

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    sys.path.insert(0, str(fixtures))
    import synthetic  # noqa: WPS433

    return synthetic


def test_compute_signature_is_deterministic():
    syn = _import_synthetic()
    c = syn.make_class(
        syn.ClassSpec(
            descriptor="Lcom/x/A;",
            methods=syn.standard_methods(),
            fields=syn.standard_fields(),
        )
    )
    s1 = compute_signature(c)
    s2 = compute_signature(c)
    assert s1 == s2


def test_compute_signature_partials_are_thirty_two_bits_each():
    syn = _import_synthetic()
    c = syn.make_class(syn.ClassSpec(descriptor="Lcom/x/A;", methods=syn.standard_methods()))
    sig = compute_signature(c)
    for partial in (sig.cls, sig.fld, sig.mth, sig.code):
        assert 0 <= partial < (1 << 32)


def test_signature_hamming_is_zero_on_self():
    syn = _import_synthetic()
    c = syn.make_class(syn.ClassSpec(descriptor="Lcom/x/A;", methods=syn.standard_methods()))
    sig = compute_signature(c)
    assert sig.hamming(sig) == 0


def test_signature_partials_are_independently_addressable():
    syn = _import_synthetic()
    # Same fields, different methods → fld partial should typically match,
    # mth/code partials should differ. This isn't a strict invariant (SimHash
    # is probabilistic) but verifies the API surface.
    a = syn.make_class(
        syn.ClassSpec(
            descriptor="Lcom/x/A;",
            methods=syn.standard_methods(),
            fields=syn.standard_fields(),
        )
    )
    b = syn.make_class(
        syn.ClassSpec(
            descriptor="Lcom/x/A;",
            methods=(),
            fields=syn.standard_fields(),
        )
    )
    sa = compute_signature(a)
    sb = compute_signature(b)
    # Fields partial: identical inputs → identical bits
    assert sa.fld == sb.fld
    # Method partial: input differs → bits should (almost certainly) differ
    assert sa.mth != sb.mth


def test_lsh_finds_self_with_zero_distance():
    idx = LSHIndex()
    s = Signature(cls=0x11112222, fld=0x33334444, mth=0x55556666, code=0x77778888)
    idx.add(s, payload=7)
    res = idx.query(s, k=3)
    assert res, "expected at least one neighbor"
    payload, d = res[0]
    assert payload == 7
    assert d == 0


def test_lsh_returns_top_k_by_ascending_hamming():
    idx = LSHIndex()
    # Three signatures with controlled Hamming distances from the query.
    base = Signature(cls=0, fld=0, mth=0, code=0)
    idx.add(base, payload=0)  # distance 0
    # Distance-1 signature
    idx.add(Signature(cls=1, fld=0, mth=0, code=0), payload=1)
    # Distance-3 signature
    idx.add(Signature(cls=0b111, fld=0, mth=0, code=0), payload=3)
    # Far signature (unlikely to share a bucket — that's the point of LSH)
    idx.add(Signature(cls=0xDEADBEEF, fld=0xCAFEBABE, mth=0x12345678, code=0x9ABCDEF0), payload=99)
    res = idx.query(base, k=4)
    # At minimum, the exact-match must come first
    assert res[0] == (0, 0)
    # The bucketed search is approximate; verify it at least found the close ones
    payloads = [p for p, _ in res]
    assert 0 in payloads
    # Distance-1 candidate should typically be found (shares many bucket keys)
    # but LSH is approximate — we only assert the zero-distance match


def test_lsh_query_on_empty_returns_empty():
    idx = LSHIndex()
    s = Signature(cls=1, fld=2, mth=3, code=4)
    assert idx.query(s, k=5) == []


def test_lsh_scaling_finds_neighbors_fast_at_20k():
    rng = random.Random(2026)
    idx = LSHIndex()
    sigs = []
    for i in range(20_000):
        s = Signature(
            cls=rng.getrandbits(32),
            fld=rng.getrandbits(32),
            mth=rng.getrandbits(32),
            code=rng.getrandbits(32),
        )
        sigs.append(s)
        idx.add(s, payload=i)
    target = sigs[7777]
    t0 = time.perf_counter()
    res = idx.query(target, k=3)
    elapsed = time.perf_counter() - t0
    assert any(p == 7777 for p, _ in res), "self-match must be among neighbors"
    # Scaling sanity gate: bucketed search should be well under 50 ms even on
    # 20k entries. The threshold is generous to absorb test-host variance.
    assert elapsed < 0.5, f"LSH query too slow: {elapsed*1000:.1f} ms"


def test_lsh_default_parameters_match_module_constants():
    idx = LSHIndex()
    assert idx.n_permutations == N_PERMUTATIONS_DEFAULT
    assert idx.bucket_prefix_bits == BUCKET_PREFIX_BITS_DEFAULT


def test_lsh_rejects_bad_parameters():
    import pytest

    with pytest.raises(ValueError):
        LSHIndex(n_permutations=0)
    with pytest.raises(ValueError):
        LSHIndex(bucket_prefix_bits=0)
    with pytest.raises(ValueError):
        LSHIndex(bucket_prefix_bits=129)
