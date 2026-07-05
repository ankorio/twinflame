from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import api, manifest, report
from .model import DiffOptions


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apkdiff",
        description="DEX-level Android APK class-diffing engine.",
    )
    p.add_argument("apk1", type=Path, nargs="?", help="left-hand-side input (original): an APK, a .dex file, or a directory of .dex files")
    p.add_argument("apk2", type=Path, nargs="?", help="right-hand-side input (modified): an APK, a .dex file, or a directory of .dex files")
    p.add_argument("--dex1", type=Path, nargs="+", metavar="PATH", help="explicit .dex file(s)/dir(s) for the left side (dumped content, no APK); overrides apk1")
    p.add_argument("--dex2", type=Path, nargs="+", metavar="PATH", help="explicit .dex file(s)/dir(s) for the right side; overrides apk2")

    pkg = p.add_mutually_exclusive_group()
    pkg.add_argument("--package", metavar="PREFIX", help="restrict to classes under this package prefix (includes subpackages)")
    pkg.add_argument("--auto-package", action="store_true", help="derive the dev package from lhs AndroidManifest.xml and use it as --package")

    p.add_argument("--normalize", action="store_true", help="run Redex LocalDcePass+RegAllocPass on both APKs first")
    p.add_argument("--no-cluster", action="store_true", help="disable Stage-1 package clustering (one global pool)")
    p.add_argument("--neighbors", "-k", type=int, default=3, help="top-k Stage-2 neighbors retained for Stage 3")
    p.add_argument("--buckets", "-n", type=int, default=16, help="number of LSH permutations / buckets")
    p.add_argument("--threshold", "-t", type=float, default=0.8, help="minimum similarity to report as a match")
    p.add_argument("--min-instr", type=int, default=DiffOptions().min_inst_size_threshold, help="skip classes with fewer than N total instructions")
    p.add_argument("--skip-synthetic", dest="skip_synthetic", action="store_true", default=True)
    p.add_argument("--no-skip-synthetic", dest="skip_synthetic", action="store_false")
    p.add_argument("--keep-boilerplate", dest="skip_boilerplate", action="store_false", default=True, help="keep generated structural twins (tiny Comparator lambdas etc.); by default they are skipped as review noise")
    p.add_argument("--skip-inner", action="store_true", help="skip inner classes (name contains '$')")
    p.add_argument("--skip-external", action="store_true", help="skip external/framework classes (no bytecode)")
    p.add_argument("--find-obfuscated", action="store_true", help="route obfuscated-looking packages into a single fallback pool")
    p.add_argument("--no-anchors", dest="anchoring", action="store_false", default=True, help="disable Stage-B anchoring (string/framework-call seed matches)")
    p.add_argument("--no-propagation", dest="propagation", action="store_false", default=True, help="disable type-graph match propagation (superclass/interfaces/field & method types)")
    p.add_argument(
        "--assignment", choices=("greedy", "hungarian", "auto"), default="auto",
        help="1-to-1 candidate assignment strategy (default: auto — Hungarian "
        "only on small, genuinely ambiguous pools; greedy everywhere else)",
    )
    p.add_argument("--progress", action="store_true", help="print per-pool / per-batch diff progress to stderr")
    p.add_argument("--app-package", metavar="PREFIX", action="append", default=None, help="package prefix(es) owned by the app, for provenance ranking only (does NOT filter scope; repeatable for multi-root apps). Defaults to --package, else the manifest package. App changes rank above library churn in --changes.")
    p.add_argument("--changes", action="store_true", help="output a semantic change report (added/removed/modified/cosmetic, ranked by review-worthiness) instead of the raw class-match list")
    p.add_argument("--min-confidence", choices=("high", "low"), default="low", help="drop low-confidence 'modified' verdicts (call-only churn in non-app code, likely cross-toolchain noise). 'low' (default) keeps everything; 'high' shows only trustworthy changes")
    p.add_argument("--changes-json", metavar="OUT", type=Path, help="write the semantic change report as machine-consumable JSON to this path")
    p.add_argument("--json", metavar="OUT", type=Path, help="also write JSON report to this path")
    p.add_argument("--deobfuscation-map", metavar="OUT", type=Path, help="write a ProGuard mapping.txt that renames apk2's obfuscated classes using names recovered from matched apk1 classes (cross-version propagation)")
    p.add_argument("--map-min-confidence", type=float, default=0.8, help="minimum match similarity to include a class in the deobfuscation map (anchored matches always included)")
    p.add_argument("--jobs", "-j", type=int, default=max(1, os.cpu_count() or 1), help="parallel workers (hard cap)")
    return p


def _load_side(positional, dex_paths, *, redex_normalize: bool, n: int):
    """Resolve one side's input into an `App`. Priority: explicit --dexN list >
    positional. A positional that is a directory or a .dex file is loaded as raw
    DEX (dumped content); otherwise it's parsed as an APK."""
    if dex_paths:
        return api.load_dex(dex_paths)
    if positional is None:
        raise SystemExit(f"error: side {n} has no input — pass an APK/.dex/dir positionally, or --dex{n} <paths>")
    p = Path(positional)
    if p.is_dir() or p.suffix.lower() == ".dex":
        return api.load_dex([p])
    return api.load(p, redex_normalize=redex_normalize)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    t_load_start = time.perf_counter()
    lhs_app = _load_side(args.apk1, args.dex1, redex_normalize=args.normalize, n=1)
    rhs_app = _load_side(args.apk2, args.dex2, redex_normalize=args.normalize, n=2)
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
        "skip_boilerplate": args.skip_boilerplate,
        "find_obfuscated_packages": args.find_obfuscated,
        "min_inst_size_threshold": args.min_instr,
        "top_match_threshold": args.neighbors,
        "buckets": args.buckets,
        "jobs": args.jobs,
        "cluster": not args.no_cluster,
        "anchoring": args.anchoring,
        "propagation": args.propagation,
        "assignment": args.assignment,
        "progress": args.progress,
    }

    t_diff_start = time.perf_counter()
    matches = api.diff(lhs_classes, rhs_classes, args.threshold, optimizations)
    t_diff = time.perf_counter() - t_diff_start
    print(f"diff: {t_diff:.2f}s ({len(matches)} matches)", file=sys.stderr)

    change_list = None
    if args.changes or args.changes_json:
        from . import changes as changes_mod

        # Provenance labeling wants the app's own package prefix(es). Precedence:
        # explicit --app-package (multi-root ok) > --package scope > manifest
        # package. Labeling never filters scope, so app/library ranking works on
        # a full diff without forcing the user to narrow it.
        label_packages = args.app_package or ([package] if package else None)
        if not label_packages and lhs_app.manifest is not None and lhs_app.manifest.package:
            label_packages = [lhs_app.manifest.package]
        if label_packages:
            print(f"provenance packages: {label_packages}", file=sys.stderr)
        change_list = changes_mod.change_set(matches, dev_package=label_packages)
        if args.min_confidence != "low":
            change_list = changes_mod.filter_min_confidence(change_list, args.min_confidence)
        if args.progress:
            from collections import Counter

            print(f"changes: {dict(Counter(c.kind for c in change_list))}", file=sys.stderr)

    if args.changes:
        from . import changes as changes_mod

        print(changes_mod.render_text(change_list))
    else:
        print(report.render_human(matches))
    if args.changes_json:
        from . import changes as changes_mod

        args.changes_json.write_text(changes_mod.render_json(change_list))
    if args.json:
        args.json.write_text(report.render_json(matches))
    if args.deobfuscation_map:
        from . import deobf

        entries = deobf.build_mapping(matches, min_confidence=args.map_min_confidence)
        args.deobfuscation_map.write_text(deobf.render_mapping(entries))
        print(
            f"deobfuscation-map: {len(entries)} classes -> {args.deobfuscation_map}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
