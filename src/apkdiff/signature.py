from __future__ import annotations

import hashlib
import math
import random
from typing import Iterable

from .model import AccessFlag, Class, Signature
from .opcodes import categorize

PARTIAL_BITS = 32
SIGNATURE_BITS = 128
N_PERMUTATIONS_DEFAULT = 16
BUCKET_PREFIX_BITS_DEFAULT = 16
DEFAULT_SEED = 0xA9C1D


def _hash_token(token: str) -> int:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big")


def _simhash(features: Iterable[tuple[str, float]]) -> int:
    acc = [0.0] * PARTIAL_BITS
    seen_any = False
    for token, weight in features:
        seen_any = True
        h = _hash_token(token)
        for i in range(PARTIAL_BITS):
            acc[i] += weight if (h >> i) & 1 else -weight
    if not seen_any:
        return 0
    result = 0
    for i in range(PARTIAL_BITS):
        if acc[i] > 0:
            result |= 1 << i
    return result


def _class_features(c: Class) -> list[tuple[str, float]]:
    feats: list[tuple[str, float]] = [
        (f"nmethods={len(c.methods)}", 1.0),
        (f"nfields={len(c.fields)}", 1.0),
    ]
    for flag in AccessFlag:
        if c.access & flag:
            feats.append((f"access={flag.name}", 1.0))
    return feats


def _field_features(c: Class) -> list[tuple[str, float]]:
    feats: list[tuple[str, float]] = []
    for f in c.fields:
        feats.append((f"ftype={f.type_desc}", 1.0))
        feats.append((f"fstatic={bool(f.access & AccessFlag.STATIC)}", 1.0))
    return feats


def _method_features(c: Class) -> list[tuple[str, float]]:
    feats: list[tuple[str, float]] = []
    for m in c.methods:
        w = math.log2(2 + m.instr_count)
        feats.append((f"margs={m.arg_count}", w))
        feats.append((f"mret={m.return_type}", w))
        feats.append((f"mxref={m.xref_count}", w))
    return feats


def _code_features(c: Class) -> list[tuple[str, float]]:
    feats: list[tuple[str, float]] = []
    for m in c.methods:
        if not m.bytecode:
            continue
        w = math.log2(2 + m.instr_count)
        for cat in categorize(m.bytecode):
            feats.append((f"op={cat}", w))
    return feats


def compute_signature(c: Class) -> Signature:
    return Signature(
        cls=_simhash(_class_features(c)),
        fld=_simhash(_field_features(c)),
        mth=_simhash(_method_features(c)),
        code=_simhash(_code_features(c)),
    )


def _gen_permutations(n: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    perms: list[list[int]] = []
    for _ in range(n):
        p = list(range(SIGNATURE_BITS))
        rng.shuffle(p)
        perms.append(p)
    return perms


def _apply_permutation(value: int, perm: list[int]) -> int:
    out = 0
    for i, src in enumerate(perm):
        if (value >> src) & 1:
            out |= 1 << i
    return out


class LSHIndex:
    """Bucketed nearest-neighbor index for 128-bit signatures.

    Storage: dict[(permutation_idx, leading_bits)] -> list of
    (payload, original_combined_sig). On query, the same permutations are
    applied to the query, buckets are looked up by (permutation_idx,
    leading_bits), and true Hamming distance is computed on the *original*
    (un-permuted) signatures.
    """

    __slots__ = ("n_permutations", "bucket_prefix_bits", "_perms", "_buckets")

    def __init__(
        self,
        n_permutations: int = N_PERMUTATIONS_DEFAULT,
        bucket_prefix_bits: int = BUCKET_PREFIX_BITS_DEFAULT,
        seed: int = DEFAULT_SEED,
    ) -> None:
        if n_permutations <= 0:
            raise ValueError("n_permutations must be > 0")
        if not 1 <= bucket_prefix_bits <= SIGNATURE_BITS:
            raise ValueError("bucket_prefix_bits out of range")
        self.n_permutations = n_permutations
        self.bucket_prefix_bits = bucket_prefix_bits
        self._perms = _gen_permutations(n_permutations, seed)
        self._buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def _prefix(self, permuted: int) -> int:
        shift = SIGNATURE_BITS - self.bucket_prefix_bits
        return (permuted >> shift) & ((1 << self.bucket_prefix_bits) - 1)

    def add(self, sig: Signature, payload: int) -> None:
        v = sig.combined
        for idx, perm in enumerate(self._perms):
            pv = _apply_permutation(v, perm)
            key = (idx, self._prefix(pv))
            self._buckets.setdefault(key, []).append((payload, v))

    def query(self, sig: Signature, k: int = 3) -> list[tuple[int, int]]:
        v = sig.combined
        seen: dict[int, int] = {}
        for idx, perm in enumerate(self._perms):
            pv = _apply_permutation(v, perm)
            key = (idx, self._prefix(pv))
            bucket = self._buckets.get(key)
            if not bucket:
                continue
            for payload, orig in bucket:
                d = (orig ^ v).bit_count()
                if payload in seen:
                    if d < seen[payload]:
                        seen[payload] = d
                else:
                    seen[payload] = d
        if not seen:
            return []
        ranked = sorted(seen.items(), key=lambda kv: kv[1])
        return ranked[:k]
