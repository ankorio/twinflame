from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import api, manifest, report


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apkdiff",
        description="DEX-level Android APK class-diffing engine.",
    )
    p.add_argument("apk1", type=Path, help="left-hand-side APK (original)")
    p.add_argument("apk2", type=Path, help="right-hand-side APK (modified)")

    pkg = p.add_mutually_exclusive_group()
    pkg.add_argument("--package", metavar="PREFIX", help="restrict to classes under this package prefix (includes subpackages)")
    pkg.add_argument("--auto-package", action="store_true", help="derive the dev package from lhs AndroidManifest.xml and use it as --package")

    p.add_argument("--normalize", action="store_true", help="run Redex LocalDcePass+RegAllocPass on both APKs first")
    p.add_argument("--no-cluster", action="store_true", help="disable Stage-1 package clustering (one global pool)")
    p.add_argument("--neighbors", "-k", type=int, default=3, help="top-k Stage-2 neighbors retained for Stage 3")
    p.add_argument("--buckets", "-n", type=int, default=16, help="number of LSH permutations / buckets")
    p.add_argument("--threshold", "-t", type=float, default=0.8, help="minimum similarity to report as a match")
    p.add_argument("--min-instr", type=int, default=5, help="skip classes with fewer than N total instructions")
    p.add_argument("--skip-synthetic", dest="skip_synthetic", action="store_true", default=True)
    p.add_argument("--no-skip-synthetic", dest="skip_synthetic", action="store_false")
    p.add_argument("--skip-inner", action="store_true", help="skip inner classes (name contains '$')")
    p.add_argument("--skip-external", action="store_true", help="skip external/framework classes (no bytecode)")
    p.add_argument("--find-obfuscated", action="store_true", help="route obfuscated-looking packages into a single fallback pool")
    p.add_argument("--json", metavar="OUT", type=Path, help="also write JSON report to this path")
    p.add_argument("--jobs", "-j", type=int, default=max(1, os.cpu_count() or 1), help="parallel workers (hard cap)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    t_load_start = time.perf_counter()
    lhs_app = api.load(args.apk1, redex_normalize=args.normalize)
    rhs_app = api.load(args.apk2, redex_normalize=args.normalize)
    t_load = time.perf_counter() - t_load_start
    print(f"load: {t_load:.2f}s ({len(lhs_app.classes)} lhs / {len(rhs_app.classes)} rhs classes)", file=sys.stderr)

    package = args.package
    if args.auto_package and lhs_app.manifest is not None:
        package = manifest.suggest_package_prefix(lhs_app.manifest)
        print(f"auto-package: {package}", file=sys.stderr)

    condition = {"package_filtering": package} if package else None
    lhs_classes = api.filter(lhs_app.classes, condition)
    rhs_classes = api.filter(rhs_app.classes, condition)

    optimizations = {
        "inner_skipping": args.skip_inner,
        "external_skipping": args.skip_external,
        "synthetic_skipping": args.skip_synthetic,
        "find_obfuscated_packages": args.find_obfuscated,
        "min_inst_size_threshold": args.min_instr,
        "top_match_threshold": args.neighbors,
        "buckets": args.buckets,
        "jobs": args.jobs,
        "cluster": not args.no_cluster,
    }

    t_diff_start = time.perf_counter()
    matches = api.diff(lhs_classes, rhs_classes, args.threshold, optimizations)
    t_diff = time.perf_counter() - t_diff_start
    print(f"diff: {t_diff:.2f}s ({len(matches)} matches)", file=sys.stderr)

    print(report.render_human(matches))
    if args.json:
        args.json.write_text(report.render_json(matches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
