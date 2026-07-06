from __future__ import annotations

import random

import numpy as np

from twinflame._hot import (
    hamming_pairs,
    levenshtein_bytes,
    popcount_int,
    popcount_u32,
)


def test_popcount_u32_matches_bit_count_on_random_input():
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 2**32, size=10_000, dtype=np.uint64).astype(np.uint32)
    expected = np.array([int(v).bit_count() for v in arr], dtype=np.uint8)
    np.testing.assert_array_equal(popcount_u32(arr), expected)


def test_popcount_u32_handles_boundary_values():
    arr = np.array([0, 1, 0x7FFFFFFF, 0xFFFFFFFF], dtype=np.uint32)
    np.testing.assert_array_equal(popcount_u32(arr), np.array([0, 1, 31, 32], dtype=np.uint8))


def test_popcount_int_matches_int_bit_count():
    rng = random.Random(0xC0DE)
    for _ in range(1000):
        v = rng.getrandbits(128)
        assert popcount_int(v) == v.bit_count()


def test_hamming_pairs_zero_distance_with_self():
    arr = np.array([0xDEADBEEF, 0x12345678, 0x0], dtype=np.uint32)
    for q in arr.tolist():
        d = hamming_pairs(int(q), arr)
        assert d[arr.tolist().index(q)] == 0


def test_hamming_pairs_matches_xor_popcount():
    rng = np.random.default_rng(7)
    candidates = rng.integers(0, 2**32, size=500, dtype=np.uint64).astype(np.uint32)
    query = int(rng.integers(0, 2**32))
    expected = np.array([int(c ^ query).bit_count() for c in candidates], dtype=np.uint8)
    np.testing.assert_array_equal(hamming_pairs(query, candidates), expected)


def test_levenshtein_classic_examples():
    assert levenshtein_bytes(b"kitten", b"sitting") == 3
    assert levenshtein_bytes(b"flaw", b"lawn") == 2
    assert levenshtein_bytes(b"abc", b"abc") == 0


def test_levenshtein_empty_cases():
    assert levenshtein_bytes(b"", b"") == 0
    assert levenshtein_bytes(b"", b"abc") == 3
    assert levenshtein_bytes(b"abc", b"") == 3


def test_levenshtein_is_symmetric():
    rng = random.Random(99)
    for _ in range(50):
        la = rng.randint(0, 30)
        lb = rng.randint(0, 30)
        a = bytes(rng.randint(0, 12) for _ in range(la))
        b = bytes(rng.randint(0, 12) for _ in range(lb))
        assert levenshtein_bytes(a, b) == levenshtein_bytes(b, a)


def test_levenshtein_triangle_inequality():
    rng = random.Random(1)
    for _ in range(20):
        a = bytes(rng.randint(0, 12) for _ in range(rng.randint(0, 15)))
        b = bytes(rng.randint(0, 12) for _ in range(rng.randint(0, 15)))
        c = bytes(rng.randint(0, 12) for _ in range(rng.randint(0, 15)))
        ab = levenshtein_bytes(a, b)
        bc = levenshtein_bytes(b, c)
        ac = levenshtein_bytes(a, c)
        assert ac <= ab + bc
