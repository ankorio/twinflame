"""Benchmark the current twinflame loader/matcher across the whole OSS matrix.

For every project it grades, with the mapping.txt-join oracle (eval.xversion
semantics), the runs that matter for calibration:

  * cross-version at a fixed profile  -> UC1 (version change)
  * cross-profile at a fixed version  -> obfuscation axis (lax/optimize/strict)

Emits precision/recall/F1 plus load and diff wall time for each. Reads the
persistent tree corpus/oss_matrix/<project>/<version>/<profile>/.

Usage: python bench_matrix.py [--markdown]
"""
from __future__ import annotations
import sys, time, itertools
from pathlib import Path

sys.path.insert(0, "/mnt/vault/Reversing/apkdiff/twinflame")
from twinflame import api, load
from eval.xversion import parse_class_mapping, build_cross_version_truth
from eval.score import score_class_matches

MATRIX = Path("/mnt/vault/Reversing/apkdiff/corpus/oss_matrix")
PROFILES = ["lax", "optimize", "strict"]
PKG = {"clock": "com.best.deskclock",
       "contacts": "org.fossify.contacts",
       "calc": "com.vagujhelyigergely.calculatorm3"}

_cache: dict[Path, object] = {}
def _load(p: Path):
    t0 = time.perf_counter()
    if p not in _cache:
        _cache[p] = load(str(p))
    return _cache[p], time.perf_counter() - t0

def grade(apk_x, map_x, apk_y, map_y, pkg):
    truth = build_cross_version_truth(
        parse_class_mapping(Path(map_x).read_text()),
        parse_class_mapping(Path(map_y).read_text()),
        package=pkg, exclude_synthetic_like=True)
    (ax, lx), (ay, ly) = _load(apk_x), _load(apk_y)
    t1 = time.perf_counter()
    matches = api.diff(list(ax.classes), list(ay.classes), 0.8,
                       {"cluster": False, "assignment": "auto"})
    t_diff = time.perf_counter() - t1
    r = score_class_matches(matches, truth)
    return r, lx + ly, t_diff, len(truth)

def versions(project):
    d = MATRIX / project
    return sorted([v.name for v in d.iterdir() if v.is_dir()]) if d.exists() else []

def cell(project, va, pa, vb, pb):
    base = MATRIX / project
    return (base/va/pa/"app.apk", base/va/pa/"mapping.txt",
            base/vb/pb/"app.apk", base/vb/pb/"mapping.txt")

def main():
    md = "--markdown" in sys.argv
    rows = []
    for project, pkg in PKG.items():
        vs = versions(project)
        if len(vs) < 2:
            continue
        va, vb = vs[0], vs[-1]
        # UC1: cross-version at each profile
        for prof in PROFILES:
            fx = cell(project, va, prof, vb, prof)
            if all(p.exists() for p in fx):
                r, tl, td, g = grade(*fx, pkg)
                rows.append((f"{project} {va}->{vb} @{prof}", "version(UC1)",
                             r.precision, r.recall, r.f1, tl, td, g))
        # obfuscation axis: cross-profile at version A
        for pa, pb in itertools.combinations(PROFILES, 2):
            fx = cell(project, va, pa, va, pb)
            if all(p.exists() for p in fx):
                r, tl, td, g = grade(*fx, pkg)
                rows.append((f"{project} {va} {pa}<->{pb}", "obfuscation",
                             r.precision, r.recall, r.f1, tl, td, g))

    if md:
        print("| Corpus | Axis | P | R | F1 | load s | diff s | gradable |")
        print("|---|---|---|---|---|---|---|---|")
        for n, ax, p, r, f, tl, td, g in rows:
            print(f"| {n} | {ax} | {p:.3f} | {r:.3f} | {f:.3f} | {tl:.2f} | {td:.2f} | {g} |")
    else:
        print(f"{'corpus':34s} {'axis':13s} {'P':>6}{'R':>7}{'F1':>7}{'load':>7}{'diff':>7}  grad")
        for n, ax, p, r, f, tl, td, g in rows:
            print(f"{n:34s} {ax:13s} {p:6.3f} {r:6.3f} {f:6.3f} {tl:6.2f} {td:6.2f}  {g}")

if __name__ == "__main__":
    main()
