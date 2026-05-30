from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from .accurate import class_similarity, greedy_assign
from .cluster import ClusterOptions, build_pools
from .model import App, Class, DiffOptions, Match, Pool
from .signature import LSHIndex, compute_signature


def load(path: str | Path, *, redex_normalize: bool = False) -> App:
    from . import loader

    return loader.load(path, redex_normalize=redex_normalize)


def filter(classes: Iterable[Class], condition: Optional[dict] = None) -> list[Class]:
    if not condition:
        return list(classes)
    prefix = condition.get("package_filtering")
    if not prefix:
        return list(classes)
    return [c for c in classes if _package_matches(c.package, prefix)]


def _package_matches(pkg: str, prefix: str) -> bool:
    return pkg == prefix or pkg.startswith(prefix + ".")


def diff(
    lhs: list[Class],
    rhs: list[Class],
    threshold: float = 0.8,
    optimizations: Optional[dict] = None,
) -> list[Match]:
    opts = DiffOptions.from_dict(optimizations)
    lhs_f = _apply_filters(lhs, opts)
    rhs_f = _apply_filters(rhs, opts)

    cluster_opts = ClusterOptions(
        enable=opts.cluster,
        find_obfuscated=opts.find_obfuscated_packages,
    )
    pools = build_pools(lhs_f, rhs_f, cluster_opts)
    matches: list[Match] = []
    for pool in pools:
        matches.extend(_diff_pool(pool, opts, threshold))
    return matches


def _apply_filters(classes: list[Class], opts: DiffOptions) -> list[Class]:
    out: list[Class] = []
    for c in classes:
        if opts.synthetic_skipping and c.is_synthetic:
            continue
        if opts.inner_skipping and c.is_inner:
            continue
        if opts.external_skipping and c.is_external:
            continue
        if opts.min_inst_size_threshold and c.total_instructions < opts.min_inst_size_threshold:
            continue
        out.append(c)
    return out


def _diff_pool(pool: Pool, opts: DiffOptions, threshold: float) -> list[Match]:
    matches: list[Match] = []
    if not pool.lhs and not pool.rhs:
        return matches
    if not pool.rhs:
        return [Match(lhs=c, rhs=None, distance=0.0) for c in pool.lhs]
    if not pool.lhs:
        return [Match(lhs=None, rhs=c, distance=0.0) for c in pool.rhs]

    rhs_sigs = [compute_signature(c) for c in pool.rhs]
    index = LSHIndex(n_permutations=opts.buckets)
    for i, sig in enumerate(rhs_sigs):
        index.add(sig, payload=i)

    # For each lhs class, get top-k candidates and score them with accurate
    # comparison. Collect (lhs_idx -> [(rhs_idx, score), ...]) for greedy
    # assignment within the pool.
    candidates: dict[int, list[tuple[int, float]]] = {}
    breakdowns: dict[tuple[int, int], dict[str, float]] = {}
    for li, lc in enumerate(pool.lhs):
        lsig = compute_signature(lc)
        neighbors = index.query(lsig, k=opts.top_match_threshold)
        ranked: list[tuple[int, float]] = []
        for rhs_idx, _hamming in neighbors:
            score, breakdown = class_similarity(lc, pool.rhs[rhs_idx])
            ranked.append((rhs_idx, score))
            breakdowns[(li, rhs_idx)] = breakdown
        ranked.sort(key=lambda t: -t[1])
        candidates[li] = ranked

    assigned = greedy_assign(candidates)
    claimed_lhs = set(assigned.keys())
    claimed_rhs = {rhs_idx for rhs_idx, _ in assigned.values()}

    for li, (ri, score) in assigned.items():
        if score < threshold:
            # Below threshold → treat lhs as deleted, rhs as added (no claim).
            continue
        matches.append(
            Match(
                lhs=pool.lhs[li],
                rhs=pool.rhs[ri],
                distance=score,
                breakdown=breakdowns.get((li, ri), {}),
            )
        )

    paired_lhs = {li for li, (_ri, s) in assigned.items() if s >= threshold}
    paired_rhs = {ri for li, (ri, s) in assigned.items() if s >= threshold}
    for li, c in enumerate(pool.lhs):
        if li not in paired_lhs:
            matches.append(Match(lhs=c, rhs=None, distance=0.0))
    for ri, c in enumerate(pool.rhs):
        if ri not in paired_rhs:
            matches.append(Match(lhs=None, rhs=c, distance=0.0))
    # Suppress unused-warning helpers
    _ = claimed_lhs
    _ = claimed_rhs
    return matches
