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
        epilog=(
            "subcommands:\n"
            "  twinflame prepare <sample>   fingerprint a sample once into a reusable\n"
            "                               .tfr record (skips the expensive parse on\n"
            "                               every later comparison); see prepare --help\n"
            "  twinflame compare <a> <b>    alias of the default diff — reads .tfr\n"
            "                               records, APKs, .dex, or any mix\n"
            "  twinflame score <fam> <cand> [WIP] Tier-1 containment score (family\n"
            "                               ⊆ candidate) for kinship triage —\n"
            "                               experimental, uncalibrated; see\n"
            "                               score --help\n"
            "  twinflame migrate <paths>    refresh stale/legacy records in place\n"
            "                               (no re-parse) where possible; see\n"
            "                               migrate --help\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Inputs: each is an APK, a .dex file, a directory of .dex files, or a
    # prepared .tfr record — detected by kind, no flag needed (feedback #5).
    p.add_argument("apk1", type=Path, nargs="?",
                   help="original build: an APK, a .dex file, a directory of .dex files, "
                        "or a prepared .tfr record")
    p.add_argument("apk2", type=Path, nargs="?",
                   help="modified build: same input kinds as apk1")

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
    scope.add_argument("--libsigs", metavar="PACK", default=None,
                       help="known-library catalogue pack (.tflp) for evidence-based library "
                            "labeling; defaults to $TWINFLAME_LIBSIGS or "
                            "~/.cache/twinflame/libsigs.tflp when present")
    scope.add_argument("--no-libsigs", action="store_true",
                       help="disable library-dictionary labeling even if a pack is discoverable")
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


def _resolve_side(positional, redex_normalize: bool):
    """Resolve one side's input into an `App`. A prepared `.tfr` record is
    loaded directly (no parse); a directory or a .dex file is loaded as raw
    DEX (dumped content); otherwise it's parsed as an APK. Module-level (not
    nested) so the parallel path can send it to a worker process; raises
    `LoadError` — the caller turns that into a clean exit."""
    from .loader import LoadError
    from .prepare import is_record_file, load_record

    p = Path(positional)
    if is_record_file(p):
        try:
            return load_record(p).app
        except ValueError as e:  # stale algo_version / unsupported layout
            raise LoadError(str(e))
    if p.is_dir() or p.suffix.lower() == ".dex":
        return api.load_dex([p])
    return api.load(p, redex_normalize=redex_normalize)


def _load_sides(args):
    """Load both inputs, in two worker processes when allowed (`-j` >= 2).

    Parsing dominates wall time and the sides are independent, so the
    parallel path roughly halves the load phase. Falls back to in-process
    sequential loading under `-j 1` or if the pool can't deliver (e.g. a
    worker killed by the OOM killer)."""
    from .loader import LoadError

    for n, positional in ((1, args.apk1), (2, args.apk2)):
        if positional is None:
            raise SystemExit(f"error: side {n} has no input — pass an APK, a .dex file, or a directory of .dex files")

    if args.jobs >= 2:
        from concurrent.futures import ProcessPoolExecutor
        from concurrent.futures.process import BrokenProcessPool

        try:
            with ProcessPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(_resolve_side, side, args.normalize)
                           for side in (args.apk1, args.apk2)]
                apps = []
                for n, fut in enumerate(futures, 1):
                    try:
                        apps.append(fut.result())
                    except LoadError as e:
                        raise SystemExit(f"error: side {n}: {e}")
            return apps[0], apps[1]
        except BrokenProcessPool:
            print("load: worker pool died, retrying sequentially", file=sys.stderr)

    apps = []
    for n, side in ((1, args.apk1), (2, args.apk2)):
        try:
            apps.append(_resolve_side(side, args.normalize))
        except LoadError as e:
            raise SystemExit(f"error: side {n}: {e}")
    return apps[0], apps[1]


def _default_output(apk1: Path, apk2: Path, ext: str) -> Path:
    return Path(f"{Path(apk1).stem}__vs__{Path(apk2).stem}.diff.{ext}")


def _write_output(path: Path, text: str) -> None:
    """Write an output file, creating any missing parent directories first so a
    user-supplied path into a not-yet-existing folder doesn't crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _main_prepare(argv: list[str]) -> int:
    """`twinflame prepare <sample>` — fingerprint one sample into a reusable,
    digest-keyed record (parse + signatures + abstract opcodes), so later
    comparisons skip the expensive parse. See the batch-scoring design."""
    p = argparse.ArgumentParser(
        prog="twinflame prepare",
        description="Fingerprint an APK/.dex/dir into a reusable comparison record.",
    )
    p.add_argument("sample", type=Path, help="an APK, a .dex file, or a directory of .dex files")
    p.add_argument("--digest", metavar="SHA256",
                   help="key the record by this digest (default: sha256 of the input)")
    p.add_argument("-o", "--output", metavar="PATH", type=Path,
                   help="record file to write (default: '<digest>.tfr' in the cwd)")
    p.add_argument("--normalize", action="store_true",
                   help="run Redex LocalDcePass+RegAllocPass before fingerprinting")
    args = p.parse_args(argv)

    # NB: import the functions directly — the package also exports a *symbol*
    # named `prepare`, which shadows the submodule under `from . import prepare`.
    from .loader import LoadError
    from .prepare import prepare as prepare_sample
    from .prepare import save as save_record

    t = time.perf_counter()
    try:
        rec = prepare_sample(args.sample, digest=args.digest, redex_normalize=args.normalize)
    except LoadError as e:  # bad input → clean exit
        raise SystemExit(f"error: {e}")
    out = args.output or Path(f"{rec.digest}.tfr")
    save_record(rec, out)
    print(f"prepare: {time.perf_counter() - t:.2f}s ({len(rec.classes)} classes, "
          f"algo {rec.algo_version})", file=sys.stderr)
    print(f"wrote: {out} (digest {rec.digest})", file=sys.stderr)
    return 0


def _main_score(argv: list[str]) -> int:
    """`twinflame score <family> <candidate>` — Tier-1 containment: how much of
    the family's code is structurally present in the candidate, as a scalar off
    prepared records (or APK/.dex, resolved like the diff path). See
    the batch-scoring design. This is the malware-triage primitive:
    confirm/score a flagged candidate against a known family seed.

    ⚠️ WORK IN PROGRESS / EXPERIMENTAL — the self-containment anchor is 1.0 and
    the score is asymmetric as intended, but a trustworthy family-vs-benign
    threshold depends on the unbuilt library dictionary (M3.3) and uncalibrated
    knobs. Emits a stderr notice on every run."""
    p = argparse.ArgumentParser(
        prog="twinflame score",
        description="[WIP/experimental] Containment score (family ⊆ candidate) off "
                    "two samples. Uncalibrated — see the batch-scoring design.",
    )
    p.add_argument("family", type=Path,
                   help="the known family/seed: a .tfr record, APK, .dex, or dir")
    p.add_argument("candidate", type=Path,
                   help="the sample under test: same input kinds as family")
    from .score import DEFAULT_MATCH_RADIUS
    p.add_argument("-R", "--radius", type=int, default=None,
                   help="Hamming radius for 'same class' on the 128-bit signature "
                        f"(default {DEFAULT_MATCH_RADIUS})")
    p.add_argument("--keep-library", dest="drop_library", action="store_false", default=True,
                   help="do NOT drop known-library classes from the family side "
                        "(default: drop them, so shared libs don't inflate the score)")
    p.add_argument("--min-shared-calls", type=int, default=0, metavar="N",
                   help="require a matched candidate class to share >= N framework calls "
                        "with the family class (precision gate; 0 = off, signature only)")
    p.add_argument("--libsigs", metavar="PACK", default=None,
                   help="known-library catalogue pack (.tflp): exclude dictionary-"
                        "recognised classes from the family side (defaults to "
                        "$TWINFLAME_LIBSIGS or ~/.cache/twinflame/libsigs.tflp)")
    p.add_argument("--no-libsigs", action="store_true",
                   help="disable the library-dictionary exclusion even if a pack is discoverable")
    p.add_argument("--evidence", type=int, metavar="N", default=0,
                   help="also print the N closest shared class pairs")
    p.add_argument("--json", action="store_true", help="emit the result as JSON")
    p.add_argument("--normalize", action="store_true",
                   help="run Redex before fingerprinting any APK/.dex input")
    args = p.parse_args(argv)

    from . import score as score_mod
    from .loader import LoadError

    print("warning: `score` is experimental/WIP — the self-containment anchor is "
          "reliable, but family-vs-benign thresholds are uncalibrated (needs a "
          "labeled family corpus). Use --libsigs to exclude known-library noise. "
          "Treat results as indicative.",
          file=sys.stderr)

    radius = args.radius if args.radius is not None else DEFAULT_MATCH_RADIUS
    t = time.perf_counter()
    try:
        fam_app = _resolve_side(args.family, args.normalize)
        cand_app = _resolve_side(args.candidate, args.normalize)
    except LoadError as e:
        raise SystemExit(f"error: {e}")

    # Dictionary-based family-side exclusion (the M3.3 noise fix).
    library_descriptors = None
    if args.libsigs and not Path(args.libsigs).is_file():
        raise SystemExit(f"error: --libsigs pack not found: {args.libsigs}")
    if not args.no_libsigs and args.drop_library:
        from . import libdict

        pack = libdict.find_pack(args.libsigs)
        if pack is not None:
            detector = libdict.load_detector(
                pack, log=lambda m: print(m, file=sys.stderr))
            if detector is not None:
                labels = libdict.label_classes(fam_app.classes, detector)
                library_descriptors = frozenset(labels)
                print(f"libsigs: excluding {libdict.summarize(labels)} "
                      f"from the family side ({pack})", file=sys.stderr)

    result = score_mod.containment(
        list(fam_app.classes), list(cand_app.classes),
        radius=radius, drop_library=args.drop_library,
        min_shared_calls=args.min_shared_calls,
        library_descriptors=library_descriptors,
    )

    if args.json:
        import json
        payload = {
            "score": round(result.score, 6),
            "present": result.present, "total": result.total,
            "weight_present": result.weight_present, "weight_total": result.weight_total,
            "radius": radius,
        }
        if args.evidence:
            payload["evidence"] = [
                {"family": f, "candidate": c, "distance": d}
                for f, c, d in result.evidence[:args.evidence]
            ]
        print(json.dumps(payload))
    else:
        print(f"containment: {result.score:.4f}  "
              f"({result.present}/{result.total} family classes present, "
              f"weight {result.weight_present}/{result.weight_total})")
        for f, c, d in result.evidence[:args.evidence]:
            print(f"  {f}  ~  {c}  (Δ{d})")
    print(f"score: {time.perf_counter() - t:.2f}s (radius {radius})", file=sys.stderr)
    return 0


def _main_migrate(argv: list[str]) -> int:
    """`twinflame migrate <paths>` — bring prepared records up to the running
    code's layer stamps without re-parsing the samples, where possible:
    signature-layer changes are recomputed from the stored abstract sequences;
    current-content legacy JSON records are re-encoded as packed .tfr.
    Extraction/abstract-layer changes require a true re-prepare and are
    reported, not guessed at."""
    p = argparse.ArgumentParser(
        prog="twinflame migrate",
        description="Refresh prepared .tfr records after an algorithm/layout change, "
                    "without re-parsing the original samples (where possible).",
    )
    p.add_argument("paths", nargs="+", type=Path,
                   help="record files, or directories scanned for *.tfr / *.tfr.json")
    p.add_argument("--delete-original", action="store_true",
                   help="remove a legacy .tfr.json after its packed replacement is written")
    args = p.parse_args(argv)

    from .prepare import is_record_file, load_record, migrate_record, save

    files: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            files += sorted(q for q in list(path.glob("*.tfr")) + list(path.glob("*.tfr.json"))
                            if is_record_file(q))
        else:
            files.append(path)
    if not files:
        raise SystemExit("error: no record files found")

    n_current = n_migrated = n_failed = 0
    for f in files:
        try:
            rec = load_record(f, check=False)
        except (ValueError, OSError) as e:
            print(f"{f}: unreadable ({e})", file=sys.stderr)
            n_failed += 1
            continue
        migrated = migrate_record(rec)
        if migrated is None:
            layers = ", ".join(rec.stale_layers())
            print(f"{f}: cannot migrate ({layers} layer changed) — re-prepare from the sample",
                  file=sys.stderr)
            n_failed += 1
            continue
        out = f.with_name(f.name[:-len(".json")]) if f.name.endswith(".tfr.json") else f
        if migrated is rec and out == f:
            n_current += 1
            continue
        try:
            save(migrated, out)
        except Exception as e:  # one bad record must not kill a bulk migrate
            print(f"{f}: failed to write ({e!r})", file=sys.stderr)
            n_failed += 1
            continue
        note = "re-encoded" if not rec.stale_layers() else "signature layer recomputed"
        print(f"{f} -> {out.name}: {note} "
              f"({f.stat().st_size / 1e6:.1f} -> {out.stat().st_size / 1e6:.1f} MB)",
              file=sys.stderr)
        if args.delete_original and out != f:
            f.unlink()
        n_migrated += 1

    print(f"migrate: {n_migrated} migrated, {n_current} already current, {n_failed} need re-prepare",
          file=sys.stderr)
    return 1 if n_failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "prepare":
        return _main_prepare(argv[1:])
    if argv and argv[0] == "score":
        return _main_score(argv[1:])
    if argv and argv[0] == "migrate":
        return _main_migrate(argv[1:])
    if argv and argv[0] == "compare":
        # Alias of the default diff, for discoverability of the prepare/compare
        # split — records, APKs, .dex and mixes all resolve per _resolve_side.
        argv = argv[1:]
    args = _build_parser().parse_args(argv)

    t_load_start = time.perf_counter()
    lhs_app, rhs_app = _load_sides(args)
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

    # Library-dictionary labeling (optional; evidence-based `library` origins).
    library_map = None
    if args.libsigs and not Path(args.libsigs).is_file():
        print(f"error: --libsigs pack not found: {args.libsigs}", file=sys.stderr)
        return 2
    if not args.no_libsigs:
        from . import libdict

        pack = libdict.find_pack(args.libsigs)
        if pack is not None:
            t_lib_start = time.perf_counter()
            detector = libdict.load_detector(
                pack, log=lambda m: print(m, file=sys.stderr))
            if detector is not None:
                library_map = libdict.label_classes(
                    list(lhs_classes) + list(rhs_classes), detector)
                print(f"libsigs: {libdict.summarize(library_map)} "
                      f"({time.perf_counter() - t_lib_start:.2f}s, {pack})",
                      file=sys.stderr)

    change_list = changes_mod.change_set(matches, dev_package=label_packages,
                                         component_map=component_map,
                                         library_map=library_map)
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
        _write_output(out_path, getattr(changes_mod, render_name)(change_list))
        print(f"wrote: {out_path} ({args.format}, {len(change_list)} classes)", file=sys.stderr)

    if args.components_file:
        _write_output(args.components_file,
                      changes_mod.render_components_text(change_list) + "\n")
        print(f"wrote: {args.components_file} (components)", file=sys.stderr)

    if args.deobfuscation_map:
        from . import deobf

        entries = deobf.build_mapping(matches, min_confidence=args.map_min_confidence)
        _write_output(args.deobfuscation_map, deobf.render_mapping(entries))
        print(f"deobfuscation-map: {len(entries)} classes -> {args.deobfuscation_map}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
