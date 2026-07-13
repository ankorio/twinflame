from __future__ import annotations

import math
import random
from typing import Iterable

from .anchor import FRAMEWORK_PREFIXES
from .model import AccessFlag, Class, Signature
from .opcodes import method_categories

try:
    import twinflame_rs as _native  # native SimHash accelerator (optional wheel)
except ImportError:  # pragma: no cover - pure-Python fallback is used below
    _native = None

PARTIAL_BITS = 32
SIGNATURE_BITS = 128
N_PERMUTATIONS_DEFAULT = 16
BUCKET_PREFIX_BITS_DEFAULT = 16
# Token mixer identity, folded into SIGNATURE_STAMP so a change invalidates
# stale records/packs. MurmurHash3 x86_32 replaced blake2b (2026-07-13): a fast
# non-cryptographic hash whose strong finalizer keeps SimHash drift on par with
# blake2b (measured; FNV-1a widened the radius tail ~2 bits and was dropped). The
# pure-Python `_hash_token` below and `twinflame_rs::hash_token` MUST stay
# bit-identical.
HASH_ALGO = "murmur3_x86_32"
# Multi-probe LSH (research note §2): a true near-neighbor can differ from the
# query in a few bits that land in the bucket prefix after permutation, flipping
# the prefix and landing it in an *adjacent* bucket. Probing prefixes within a
# small Hamming radius of the query's prefix (per permutation) recovers those
# without adding permutation tables. 0 = exact bucket only.
#
# DEFAULT 0 (off), by measurement: at N_PERMUTATIONS_DEFAULT=16 the exact-bucket
# probes already saturate near-neighbor recall (a small-Hamming neighbor keeps
# all differing bits out of the 16-bit prefix in *some* permutation with high
# probability), so radius 1 adds only ~+2pts candidate surfacing and *zero*
# end-to-end recall on all corpora (eval/corpus/README.md): release-vs-release
# pairs are already in the exact bucket; debug-vs-release extras are blocked by
# the score threshold. Multi-probe's real use is recovering recall when
# permutations are *few* (a memory/perf tradeoff) — enable it then.
PROBE_RADIUS_DEFAULT = 0
DEFAULT_SEED = 0xA9C1D

# Weights for the rename-/optimization-invariant tokens folded into the
# signature (candidate-generation diversification). Deliberately above the
# unit-weight structural features and the log-scaled opcode features: a
# framework supertype or call target survives R8 rename *and* optimization
# verbatim on both sides of an obfuscated pair, so it must pull hard enough on
# the SimHash bits to bucket true pairs together — the diagnosed 84%-not-
# surfaced gap (see eval/corpus/README.md) is dominated by true pairs whose
# structure-only signatures drifted too far apart to share any LSH bucket.
SUPER_WEIGHT = 4.0
IFACE_WEIGHT = 3.0
CALL_WEIGHT_MULT = 3.0


_MURMUR_C1 = 0xCC9E2D51
_MURMUR_C2 = 0x1B873593
_HASH_CACHE: dict[str, int] = {}


def _murmur3_32(data: bytes) -> int:
    """MurmurHash3 x86_32, seed 0. Bit-identical to `twinflame_rs::hash_token`."""
    h = 0
    n = len(data)
    nblocks = n // 4
    for i in range(nblocks):
        k = int.from_bytes(data[i * 4:i * 4 + 4], "little")
        k = (k * _MURMUR_C1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * _MURMUR_C2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[nblocks * 4:]
    k = 0
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * _MURMUR_C1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * _MURMUR_C2) & 0xFFFFFFFF
        h ^= k
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def _hash_token(token: str) -> int:
    """Murmur3-x86_32 mixer over the token's UTF-8 bytes. Memoized — tokens
    repeat 60-150x across an app (measured), so distinct-token hashing is a
    fraction of the work. Must stay bit-identical to `twinflame_rs::hash_token`."""
    v = _HASH_CACHE.get(token)
    if v is None:
        v = _HASH_CACHE[token] = _murmur3_32(token.encode("utf-8"))
    return v


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
    # Rename-invariant type-graph anchors: a *framework* superclass or interface
    # survives R8 rename+optimization verbatim on both sides of an obfuscated
    # pair, so folding it in pulls true pairs into the same LSH buckets. An
    # *app* supertype is renamed differently per build, so it is NOT invariant —
    # filter to framework prefixes (same set anchoring trusts).
    if c.superclass and c.superclass.startswith(FRAMEWORK_PREFIXES):
        feats.append((f"super={c.superclass}", SUPER_WEIGHT))
    for iface in c.interfaces:
        if iface.startswith(FRAMEWORK_PREFIXES):
            feats.append((f"iface={iface}", IFACE_WEIGHT))
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
        w = math.log2(2 + m.instr_count)
        for cat in method_categories(m):
            feats.append((f"op={cat}", w))
        # Framework/library call targets are the strongest optimization- and
        # rename-invariant behavioral fingerprint: R8 inlining reshuffles a
        # method's opcodes but preserves *which external APIs it invokes*, and
        # those descriptors can't be renamed. Weighted above the opcode
        # categories (only 13 distinct → they saturate the signature on small
        # classes) so the invariant signal actually moves bits. This is also the
        # signal the malware-family use case (UC2) leans on hardest.
        for ref in m.calls:
            if ref.startswith(FRAMEWORK_PREFIXES):
                feats.append((f"call={ref}", w * CALL_WEIGHT_MULT))
    return feats


def compute_signature(c: Class) -> Signature:
    # Prepared records carry the signature precomputed; reuse it verbatim so a
    # diff off a record is bit-identical to a diff off a fresh parse.
    if c.signature is not None:
        return c.signature
    cls_f = _class_features(c)
    fld_f = _field_features(c)
    mth_f = _method_features(c)
    code_f = _code_features(c)
    if _native is not None:
        # Native does hash + 32-bit accumulation (the ~91% hot path) with the
        # GIL released; result is bit-identical to the pure-Python branch.
        cls, fld, mth, code = _native.class_signature(cls_f, fld_f, mth_f, code_f)
        return Signature(cls=cls, fld=fld, mth=mth, code=code)
    return Signature(
        cls=_simhash(cls_f),
        fld=_simhash(fld_f),
        mth=_simhash(mth_f),
        code=_simhash(code_f),
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

    __slots__ = (
        "n_permutations", "bucket_prefix_bits", "probe_radius", "_perms", "_buckets"
    )

    def __init__(
        self,
        n_permutations: int = N_PERMUTATIONS_DEFAULT,
        bucket_prefix_bits: int = BUCKET_PREFIX_BITS_DEFAULT,
        seed: int = DEFAULT_SEED,
        probe_radius: int = PROBE_RADIUS_DEFAULT,
    ) -> None:
        if n_permutations <= 0:
            raise ValueError("n_permutations must be > 0")
        if not 1 <= bucket_prefix_bits <= SIGNATURE_BITS:
            raise ValueError("bucket_prefix_bits out of range")
        if not 0 <= probe_radius <= 2:
            raise ValueError("probe_radius must be 0, 1, or 2")
        self.n_permutations = n_permutations
        self.bucket_prefix_bits = bucket_prefix_bits
        self.probe_radius = probe_radius
        self._perms = _gen_permutations(n_permutations, seed)
        self._buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def _prefix(self, permuted: int) -> int:
        shift = SIGNATURE_BITS - self.bucket_prefix_bits
        return (permuted >> shift) & ((1 << self.bucket_prefix_bits) - 1)

    def _probe_prefixes(self, prefix: int):
        """Yield the prefix and, per `probe_radius`, its Hamming-1/2 neighbors.

        Flipping prefix bit i corresponds to a candidate whose true signature
        differs from the query in the bit permuted into prefix position i —
        exactly the near-neighbors that fell just outside the exact bucket.
        """
        yield prefix
        if self.probe_radius >= 1:
            bits = self.bucket_prefix_bits
            for i in range(bits):
                yield prefix ^ (1 << i)
            if self.probe_radius >= 2:
                for i in range(bits):
                    for j in range(i + 1, bits):
                        yield prefix ^ (1 << i) ^ (1 << j)

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
            prefix = self._prefix(pv)
            for probe in self._probe_prefixes(prefix):
                bucket = self._buckets.get((idx, probe))
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
