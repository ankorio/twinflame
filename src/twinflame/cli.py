from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import api, manifest, report
from .model import DiffOptions

# format -> (serializer attribute on `changes`, file extension)
_FORMATS = {
    "json": ("render_json", "json"),
    "csv": ("render_csv", "csv"),
    "xml": ("render_xml", "xml"),
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="twinflame",
        description="DEX-level Android APK diff engine (SimHash + LSH + abstract-opcode).",
    )
    # Inputs: each is an APK, a .dex file, or a directory of .dex files — detected
    # by kind, no flag needed (feedback #5).
    p.add_argument("apk1", type=Path, nargs="?",
                   help="original build: an APK, a .dex file, or a directory of .dex files")
    p.add_argument("apk2", type=Path, nargs="?",
                   help="modified build: an APK, a .dex file, or a directory of .dex files")

    # --- output (feedback #2, #3) ---------------------------------------------
    out = p.add_argument_group("output")
    out.add_argument("-o", "--output", metavar="PATH", type=Path,
                     help="diff file to write (default: '<apk1>__vs__<apk2>.diff.<ext>' in the cwd)")
    out.add_argument("-f", "--format", choices=tuple(_FORMATS), default="json",
                     help="diff file format (default: json)")
    out.add_argument("--no-file", action="store_true",
                     help="print the summary only; do not write a diff file")
    out.add_argument("-m", "--matches", action="store_true",
                     help="print the raw per-class match list to stdout instead of the change summary")
    out.add_argument("--components-file", metavar="PATH", type=Path,
                     help="also dump the sensitive-superclass / component report to this file")

    # --- selection / targeting (feedback #1, #7) ------------------------------
    sel = p.add_argument_group("selection")
    sel.add_argument("-s", "--select", choices=("all", "changed", "unchanged"), default="all",
                     help="which classes to emit: only-different, only-same, or all (default)")
    sel.add_argument("-c", "--class", dest="klass", metavar="NAME",
                     help="restrict the diff to classes whose name/path/descriptor contains NAME")

    # --- scope ----------------------------------------------------------------
    scope = p.add_argument_group("scope")
    pkg = scope.add_mutually_exclusive_group()
    pkg.add_argument("-p", "--package", metavar="PREFIX",
                     help="restrict to classes under this package prefix (includes subpackages)")
    pkg.add_argument("-A", "--auto-package", action="store_true",
                     help="derive the dev package from lhs AndroidManifest.xml and use it as --package")
    scope.add_argument("--app-package", metavar="PREFIX", action="append", default=None,
                       help="package prefix(es) owned by the app, for provenance ranking only "
                            "(does NOT filter scope; repeatable for multi-root apps)")
    scope.add_argument("--skip-inner", action="store_true", help="skip inner classes (name contains '$')")
    scope.add_argument("--skip-external", action="store_true", help="skip external/framework classes")
    scope.add_argument("--skip-synthetic", dest="skip_synthetic", action="store_true", default=True)
    scope.add_argument("--no-skip-synthetic", dest="skip_synthetic", action="store_false")
    scope.add_argument("--keep-boilerplate", dest="skip_boilerplate", action="store_false", default=True,
                       help="keep generated structural twins (tiny Comparator lambdas etc.)")
    scope.add_argument("--min-instr", type=int, default=DiffOptions().min_inst_size_threshold,
                       help="skip classes with fewer than N total instructions")

    # --- matching knobs -------------------------------------------------------
    tune = p.add_argument_group("matching")
    tune.add_argument("-t", "--threshold", type=float, default=0.8,
                      help="minimum similarity to report as a match")
    tune.add_argument("-k", "--neighbors", type=int, default=3, help="top-k Stage-2 neighbors kept for Stage 3")
    tune.add_argument("-n", "--buckets", type=int, default=16, help="number of LSH permutations / buckets")
    tune.add_argument("--no-cluster", action="store_true", help="disable Stage-1 package clustering (one global pool)")
    tune.add_argument("--find-obfuscated", action="store_true",
                      help="route obfuscated-looking packages into a single fallback pool")
    tune.add_argument("--no-anchors", dest="anchoring", action="store_false", default=True,
                      help="disable Stage-B anchoring (string/framework-call seed matches)")
    tune.add_argument("--no-propagation", dest="propagation", action="store_false", default=True,
                      help="disable type-graph match propagation")
    tune.add_argument("--assignment", choices=("greedy", "hungarian", "auto"), default="auto",
                      help="1-to-1 candidate assignment strategy (default: auto)")
    tune.add_argument("--normalize", action="store_true",
                      help="run Redex LocalDcePass+RegAllocPass on both APKs first")
    tune.add_argument("--min-confidence", choices=("high", "low"), default="low",
                      help="'high' drops low-confidence call-only churn in non-app code")

    # --- extras ---------------------------------------------------------------
    extra = p.add_argument_group("extras")
    extra.add_argument("--deobfuscation-map", metavar="OUT", type=Path,
                       help="write a ProGuard mapping.txt renaming apk2's classes from matched apk1 names")
    extra.add_argument("--map-min-confidence", type=float, default=0.8,
                       help="minimum match similarity to include a class in the deobfuscation map")
    extra.add_argument("-j", "--jobs", type=int, default=max(1, os.cpu_count() or 1),
                       help="parallel workers (hard cap)")
    extra.add_argument("--progress", action="store_true", help="print per-pool diff progress to stderr")
    return p


def _load_side(positional, *, redex_normalize: bool, n: int):
    """Resolve one side's input into an `App`. A directory or a .dex file is loaded
    as raw DEX (dumped content); otherwise it's parsed as an APK. Exits cleanly
    (no traceback) on bad input."""
    from .loader import LoadError

    if positional is None:
        raise SystemExit(f"error: side {n} has no input — pass an APK, a .dex file, or a directory of .dex files")
    try:
        p = Path(positional)
        if p.is_dir() or p.suffix.lower() == ".dex":
            return api.load_dex([p])
        return api.load(p, redex_normalize=redex_normalize)
    except LoadError as e:
        raise SystemExit(f"error: side {n}: {e}")


def _default_output(apk1: Path, apk2: Path, ext: str) -> Path:
    return Path(f"{Path(apk1).stem}__vs__{Path(apk2).stem}.diff.{ext}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    t_load_start = time.perf_counter()
    lhs_app = _load_side(args.apk1, redex_normalize=args.normalize, n=1)
    rhs_app = _load_side(args.apk2, redex_normalize=args.normalize, n=2)
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

    from . import changes as changes_mod
    from . import components as components_mod

    # Sensitive-component detection resolves the superclass chain across the FULL
    # app (not the filtered scope), so transitive extends still resolve.
    component_map = components_mod.merge_labels(
        components_mod.component_labels(lhs_app.classes),
        components_mod.component_labels(rhs_app.classes),
    )

    # Provenance labeling: explicit --app-package > --package scope > manifest.
    label_packages = args.app_package or ([package] if package else None)
    if not label_packages and lhs_app.manifest is not None and lhs_app.manifest.package:
        label_packages = [lhs_app.manifest.package]
    if label_packages:
        print(f"provenance packages: {label_packages}", file=sys.stderr)

    change_list = changes_mod.change_set(matches, dev_package=label_packages, component_map=component_map)
    if args.min_confidence != "low":
        change_list = changes_mod.filter_min_confidence(change_list, args.min_confidence)
    if args.klass:
        change_list = changes_mod.filter_class(change_list, args.klass)
    change_list = changes_mod.select_changes(change_list, args.select)

    # --- stdout ---------------------------------------------------------------
    if args.matches:
        print(report.render_human(matches))
    else:
        print(changes_mod.render_text(change_list))
    print(changes_mod.render_components_text(change_list))

    # --- diff file (default on; feedback #2) ----------------------------------
    if not args.no_file:
        render_name, ext = _FORMATS[args.format]
        out_path = args.output or _default_output(args.apk1, args.apk2, ext)
        out_path.write_text(getattr(changes_mod, render_name)(change_list))
        print(f"wrote: {out_path} ({args.format}, {len(change_list)} classes)", file=sys.stderr)

    if args.components_file:
        args.components_file.write_text(changes_mod.render_components_text(change_list) + "\n")
        print(f"wrote: {args.components_file} (components)", file=sys.stderr)

    if args.deobfuscation_map:
        from . import deobf

        entries = deobf.build_mapping(matches, min_confidence=args.map_min_confidence)
        args.deobfuscation_map.write_text(deobf.render_mapping(entries))
        print(f"deobfuscation-map: {len(entries)} classes -> {args.deobfuscation_map}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
