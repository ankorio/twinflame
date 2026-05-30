from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .model import Class, Pool


@dataclass(frozen=True)
class ClusterOptions:
    enable: bool = True
    find_obfuscated: bool = False
    entropy_threshold: float = 3.2
    max_segment_len: int = 3


OBFUSCATED_POOL_KEY = "__obfuscated__"
GLOBAL_POOL_KEY = "__global__"


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    h = 0.0
    for c in counts.values():
        p = c / n
        h -= p * math.log2(p)
    return h


def is_obfuscated_package(pkg: str, opts: ClusterOptions) -> bool:
    if not pkg:
        return True
    segments = [s for s in pkg.split(".") if s]
    if not segments:
        return True
    # A package is treated as obfuscated only if every dotted segment is
    # short AND low-entropy. Any clear-text segment breaks the chain — so
    # "com.acme.app" passes but "a.b.c" does not.
    for seg in segments:
        if len(seg) > opts.max_segment_len:
            return False
        if shannon_entropy(seg) >= opts.entropy_threshold:
            return False
    return True


def _pool_key(c: Class, opts: ClusterOptions) -> str:
    if not opts.enable:
        return GLOBAL_POOL_KEY
    if opts.find_obfuscated and is_obfuscated_package(c.package, opts):
        return OBFUSCATED_POOL_KEY
    return c.package or OBFUSCATED_POOL_KEY


def build_pools(
    lhs: list[Class],
    rhs: list[Class],
    opts: ClusterOptions,
) -> list[Pool]:
    lhs_by_key: dict[str, list[Class]] = defaultdict(list)
    rhs_by_key: dict[str, list[Class]] = defaultdict(list)
    for c in lhs:
        lhs_by_key[_pool_key(c, opts)].append(c)
    for c in rhs:
        rhs_by_key[_pool_key(c, opts)].append(c)
    keys = set(lhs_by_key) | set(rhs_by_key)
    return [Pool(key=k, lhs=lhs_by_key.get(k, []), rhs=rhs_by_key.get(k, [])) for k in sorted(keys)]
