from __future__ import annotations

import sys
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Iterable, Optional

from .accurate import class_similarity, compare_classes, select_assignment
from .anchor import seed_anchors
from .cluster import ClusterOptions, build_pools
from .model import App, Class, DiffOptions, Match, Pool
from .signature import LSHIndex, compute_signature


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load(path: str | Path, *, redex_normalize: bool = False) -> App:
    from . import loader

    return loader.load(path, redex_normalize=redex_normalize)


def load_dex(paths, *, label: str | None = None) -> App:
    """Load raw .dex file(s)/dir(s) into an `App` (dumped content, no APK)."""
    from . import loader

    return loader.load_dex(paths, label=label)


def _load_error():
    """The exception type raised by load/load_dex on bad input (lazy import)."""
    from .loader import LoadError

    return LoadError


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
    # Process biggest pools first so the slow ones surface immediately under
    # --progress instead of after a long quiet stretch.
    active = [p for p in pools if p.lhs and p.rhs]
    active.sort(key=lambda p: -(len(p.lhs) * len(p.rhs)))
    other = [p for p in pools if not (p.lhs and p.rhs)]
    ordered = active + other

    if opts.progress:
        _log(f"diff: {len(ordered)} pools ({len(active)} to compare); "
             f"largest {len(active[0].lhs)}x{len(active[0].rhs)}" if active
             else f"diff: {len(ordered)} pools (nothing to compare)")

    jobs = max(1, opts.jobs or 1)
    if jobs > 1 and len(active) > 1:
        matches = _diff_parallel(ordered, opts, threshold, jobs)
    else:
        matches = []
        for i, pool in enumerate(ordered, 1):
            if opts.progress and pool.lhs and pool.rhs:
                _log(f"  [pool {i}/{len(ordered)}] {pool.key or '<root>'}: "
                     f"{len(pool.lhs)}x{len(pool.rhs)}")
            matches.extend(_diff_pool(pool, opts, threshold))

    if opts.propagation:
        from .propagate import propagate_matches

        if opts.progress:
            before = sum(1 for m in matches if m.is_paired)
        matches = propagate_matches(matches, threshold=threshold, assignment=opts.assignment)
        if opts.progress:
            after = sum(1 for m in matches if m.is_paired)
            _log(f"  propagation: +{after - before} matches (type-graph cascade)")

    return matches


def _diff_parallel(
    ordered: list[Pool], opts: DiffOptions, threshold: float, jobs: int
) -> list[Match]:
    """Pool-level parallelism (plan K2).

    The single largest pool runs inline — it dominates the runtime and is too
    big to pickle to a worker cheaply — while the long tail of small pools is
    farmed across `jobs` processes. Threads would be pointless here: the hot
    path (Levenshtein) is pure-Python and GIL-bound, so only separate
    processes give real speedup.
    """
    from .parallel import run_pools

    head, tail = ordered[0], ordered[1:]
    matches: list[Match] = []

    if opts.progress:
        _log(f"  [inline] {head.key or '<root>'}: {len(head.lhs)}x{len(head.rhs)}")
    matches.extend(_diff_pool(head, opts, threshold))

    if opts.progress:
        _log(f"  dispatching {len(tail)} remaining pools across {jobs} workers…")
    # Inner per-pool progress would interleave across processes; silence it.
    worker_opts = replace(opts, progress=False)
    for result in run_pools(partial(_diff_pool, opts=worker_opts, threshold=threshold), tail, jobs=jobs):
        matches.extend(result)
    if opts.progress:
        _log("  done")
    return matches


def _apply_filters(classes: list[Class], opts: DiffOptions) -> list[Class]:
    from .boilerplate import is_boilerplate

    out: list[Class] = []
    for c in classes:
        if opts.synthetic_skipping and c.is_synthetic:
            continue
        if opts.skip_boilerplate and is_boilerplate(c):
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

    big = opts.progress and len(pool.lhs) >= 500

    # Stage B — anchors. Lock in R8-invariant seed pairs first; they win over
    # structural matching and are kept even if the body changed a lot.
    anchored: dict[int, int] = {}
    if opts.anchoring:
        if big:
            _log("      anchoring…")
        t = time.perf_counter()
        for li, ri, _conf in seed_anchors(pool.lhs, pool.rhs):
            anchored[li] = ri
        if big:
            _log(f"      anchored {len(anchored)} pairs in {time.perf_counter() - t:.1f}s")
    anchored_rhs = set(anchored.values())

    # Stage 2/3 — structural matching over the classes anchoring didn't claim.
    index = LSHIndex(n_permutations=opts.buckets, probe_radius=opts.probe_radius)
    for j, c in enumerate(pool.rhs):
        if j not in anchored_rhs:
            index.add(compute_signature(c), payload=j)

    todo = [li for li in range(len(pool.lhs)) if li not in anchored]
    candidates: dict[int, list[tuple[int, float]]] = {}
    t = time.perf_counter()
    for n, li in enumerate(todo, 1):
        lc = pool.lhs[li]
        lsig = compute_signature(lc)
        neighbors = index.query(lsig, k=opts.top_match_threshold)
        ranked = [(rj, class_similarity(lc, pool.rhs[rj])[0]) for rj, _h in neighbors]
        ranked.sort(key=lambda t: -t[1])
        candidates[li] = ranked
        if big and n % 500 == 0:
            rate = n / (time.perf_counter() - t)
            _log(f"      scored {n}/{len(todo)} classes ({rate:.0f}/s)")

    assigned = select_assignment(candidates, opts.assignment)

    paired_lhs: set[int] = set()
    paired_rhs: set[int] = set()

    # Anchored pairs: always emitted, scored at method level for the report.
    for li, ri in anchored.items():
        dist, breakdown, mms = compare_classes(pool.lhs[li], pool.rhs[ri])
        breakdown["anchored"] = 1.0
        matches.append(
            Match(pool.lhs[li], pool.rhs[ri], dist, breakdown, tuple(mms))
        )
        paired_lhs.add(li)
        paired_rhs.add(ri)

    # Structural pairs: gated by the method-level roll-up score.
    for li, (ri, _rank) in assigned.items():
        dist, breakdown, mms = compare_classes(pool.lhs[li], pool.rhs[ri])
        if dist < threshold:
            # Below threshold → leave lhs deleted / rhs added (no claim).
            continue
        matches.append(
            Match(pool.lhs[li], pool.rhs[ri], dist, breakdown, tuple(mms))
        )
        paired_lhs.add(li)
        paired_rhs.add(ri)

    for li, c in enumerate(pool.lhs):
        if li not in paired_lhs:
            matches.append(Match(lhs=c, rhs=None, distance=0.0))
    for ri, c in enumerate(pool.rhs):
        if ri not in paired_rhs:
            matches.append(Match(lhs=None, rhs=c, distance=0.0))
    return matches
