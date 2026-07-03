"""Cross-version (release-vs-release) matching scorer.

Grades apkdiff on the *real* UC1 setup: two obfuscated release builds of the
same app (adjacent versions, or the same version built twice). Neither side is
clear-named, so the oracle is built by **joining the two builds' own
`mapping.txt` files on the original class name**:

    build X:  org.foo.Bar -> a.b        (mapping_x)
    build Y:  org.foo.Bar -> c.d        (mapping_y)
    => ground truth: a.b (in X) corresponds to c.d (in Y)

A class whose original name is in *both* mappings is a gradable correspondence;
originals only in X are removed, only in Y are added (the change signal — not
scored as matching hits/misses here). This measures matching without the
debug-vs-release optimization gap that makes the rename-recovery harness
(`eval/cli.py`) an unrepresentatively hard test — see `eval/corpus/README.md`.

Usage:
    python -m eval.xversion x.apk x.mapping.txt y.apk y.mapping.txt [--package PREFIX]

The ground truth is keyed by X's *obfuscated* FQCN and valued by Y's, so the
existing `score.score_class_matches` grades it unchanged (lhs = X class, rhs =
Y class). Both sides obfuscated => a single global pool (no package clustering:
obfuscated packages don't align across versions).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apkdiff import api, load

from .mapping import is_removed_target, is_synthetic_like, parse_class_mapping
from .score import score_class_matches


def _in_package(fqcn: str, prefix: str) -> bool:
    return fqcn == prefix or fqcn.startswith(prefix + ".")


def build_cross_version_truth(
    mapping_x: dict[str, str],
    mapping_y: dict[str, str],
    *,
    package: str | None,
    exclude_synthetic_like: bool,
) -> dict[str, str]:
    """Join two original->obfuscated maps into {obf_x_fqcn: obf_y_fqcn}.

    Only originals present in *both* builds (unchanged/renamed-in-place classes)
    are gradable correspondences. Scope + synthetic filtering apply to the
    stable *original* name, exactly like the rename-recovery harness.
    """
    truth: dict[str, str] = {}
    for original, obf_x in mapping_x.items():
        obf_y = mapping_y.get(original)
        if obf_y is None:
            continue  # class was added/removed between versions — not a match target
        if is_removed_target(obf_x) or is_removed_target(obf_y):
            continue  # R8 deleted it on one side — phantom, not in the APK
        if package and not _in_package(original, package):
            continue
        if exclude_synthetic_like and is_synthetic_like(original):
            continue
        truth[obf_x] = obf_y
    return truth


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval.xversion")
    p.add_argument("apk_x", type=Path, help="older release APK (obfuscated)")
    p.add_argument("mapping_x", type=Path, help="mapping.txt for apk_x")
    p.add_argument("apk_y", type=Path, help="newer release APK (obfuscated)")
    p.add_argument("mapping_y", type=Path, help="mapping.txt for apk_y")
    p.add_argument(
        "--package", metavar="PREFIX",
        help="restrict the graded correspondences to classes whose *original* "
        "name is under this package prefix (the app's own code).",
    )
    p.add_argument("--threshold", type=float, default=0.8)
    p.add_argument(
        "--include-synthetic-like",
        dest="exclude_synthetic_like",
        action="store_false",
        default=True,
        help="grade compiler-generated bookkeeping classes too (excluded by default).",
    )
    p.add_argument("--assignment", choices=("greedy", "hungarian", "auto"), default="auto")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    mapping_x = parse_class_mapping(args.mapping_x.read_text())
    mapping_y = parse_class_mapping(args.mapping_y.read_text())
    truth = build_cross_version_truth(
        mapping_x, mapping_y,
        package=args.package,
        exclude_synthetic_like=args.exclude_synthetic_like,
    )

    app_x = load(args.apk_x)
    app_y = load(args.apk_y)
    matches = api.diff(
        list(app_x.classes), list(app_y.classes), args.threshold,
        {"cluster": False, "assignment": args.assignment},
    )
    result = score_class_matches(matches, truth)

    only_x = sum(
        1 for o in mapping_x
        if o not in mapping_y and (not args.package or _in_package(o, args.package))
        and not (args.exclude_synthetic_like and is_synthetic_like(o))
    )
    only_y = sum(
        1 for o in mapping_y
        if o not in mapping_x and (not args.package or _in_package(o, args.package))
        and not (args.exclude_synthetic_like and is_synthetic_like(o))
    )
    print(
        f"gradable correspondences (original in both builds): {len(truth)}\n"
        f"  originals only in X (removed): {only_x}   only in Y (added): {only_y}",
        file=sys.stderr,
    )
    print(f"true positives:  {result.true_positives}")
    print(f"false positives: {result.false_positives}")
    print(f"false negatives: {result.false_negatives}")
    print(f"precision: {result.precision:.4f}")
    print(f"recall:    {result.recall:.4f}")
    print(f"f1:        {result.f1:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
