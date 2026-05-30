from __future__ import annotations

import numpy as np


def _popcount8_table() -> np.ndarray:
    # Precomputed popcount for every byte value 0..255.
    return np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


_POPCOUNT8: np.ndarray = _popcount8_table()


def popcount_u32(arr: np.ndarray) -> np.ndarray:
    if arr.dtype != np.uint32:
        arr = arr.astype(np.uint32, copy=False)
    # Reinterpret as bytes (little-endian or big — order doesn't matter for popcount)
    # and sum the 4 byte-popcounts per element.
    as_bytes = arr.view(np.uint8).reshape(-1, 4)
    return _POPCOUNT8[as_bytes].sum(axis=1, dtype=np.uint8)


def hamming_pairs(query: int, candidates: np.ndarray) -> np.ndarray:
    if candidates.dtype != np.uint32:
        candidates = candidates.astype(np.uint32, copy=False)
    q = np.uint32(query & 0xFFFFFFFF)
    return popcount_u32(candidates ^ q)


def popcount_int(value: int) -> int:
    # Stand-alone for use on Python ints up to 128 bits (Signature.combined).
    return value.bit_count()


def levenshtein_bytes(a: bytes, b: bytes) -> int:
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    # Make `a` the shorter one for memory.
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    prev = list(range(la + 1))
    curr = [0] * (la + 1)
    for j in range(1, lb + 1):
        curr[0] = j
        bj = b[j - 1]
        for i in range(1, la + 1):
            cost = 0 if a[i - 1] == bj else 1
            curr[i] = min(
                curr[i - 1] + 1,
                prev[i] + 1,
                prev[i - 1] + cost,
            )
        prev, curr = curr, prev
    return prev[la]
