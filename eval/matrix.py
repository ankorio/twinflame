"""Obfuscation-profile matrix grader.

Given a directory of profile builds of the *same app version* — one subdirectory
per profile, each holding `app.apk` + `mapping.txt` (as produced by
`eval/corpus/build_profiles.sh`) — grade the matcher on every ordered profile
pair and print a precision/recall/F1 matrix.

Because all builds are the same source, two profiles share original class names,
so the cross-version join (`eval.xversion.build_cross_version_truth`) yields a
full obf↔obf oracle. Grading `lax` vs `strict` etc. measures how matching holds
as the two sides' obfuscation *diverges* — the UC2 "same code, different
obfuscation" case (malware family across obfuscators/strengths), and the
obfuscation-strength degradation curve.

Usage:
    python -m eval.matrix <profiles_dir> --package PREFIX [--assignment auto]

`<profiles_dir>` contains e.g. lax/  optimize/  strict/  (each app.apk + mapping.txt).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apkdiff import api, load

from .mapping import parse_class_mapping
from .score import ScoreResult, score_class_matches
from .xversion import build_cross_version_truth


def _grade_pair(
    classes_x, mapping_x, classes_y, mapping_y, *, package, threshold, assignment
) -> tuple[ScoreResult, int]:
    truth = build_cross_version_truth(
        mapping_x, mapping_y, package=package, exclude_synthetic_like=True
    )
    matches = api.diff(
        list(classes_x), list(classes_y), threshold,
        {"cluster": False, "assignment": assignment},
    )
    return score_class_matches(matches, truth), len(truth)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.matrix")
    p.add_argument("profiles_dir", type=Path)
    p.add_argument("--package", required=True)
    p.add_argument("--threshold", type=float, default=0.8)
    p.add_argument("--assignment", choices=("greedy", "hungarian", "auto"), default="auto")
    p.add_argument(
        "--metric", choices=("f1", "recall", "precision"), default="f1",
        help="which metric to show in the matrix cells (default f1)",
    )
    args = p.parse_args(argv)

    profiles = sorted(
        d.name for d in args.profiles_dir.iterdir()
        if (d / "app.apk").exists() and (d / "mapping.txt").exists()
    )
    if len(profiles) < 2:
        print(f"need >=2 profile builds in {args.profiles_dir}", file=sys.stderr)
        return 2

    # Load each APK + mapping once; reuse across all pairs.
    classes = {p: load(args.profiles_dir / p / "app.apk").classes for p in profiles}
    maps = {
        p: parse_class_mapping((args.profiles_dir / p / "mapping.txt").read_text())
        for p in profiles
    }

    # Grade each ordered pair once; reuse for both the matrix and the detail.
    results: dict[tuple[str, str], tuple[ScoreResult, int]] = {}
    for rx in profiles:
        for cy in profiles:
            if rx == cy:
                continue
            results[(rx, cy)] = _grade_pair(
                classes[rx], maps[rx], classes[cy], maps[cy],
                package=args.package, threshold=args.threshold, assignment=args.assignment,
            )

    print(f"profiles: {', '.join(profiles)}   (rows = X, cols = Y)")
    print(f"cell = {args.metric} of matching X against Y\n")
    print("         " + "".join(f"{c:>12}" for c in profiles))
    for rx in profiles:
        cells = []
        for cy in profiles:
            if rx == cy:
                cells.append(f"{'—':>12}")
            else:
                val = getattr(results[(rx, cy)][0], args.metric)
                cells.append(f"{val:>12.3f}")
        print(f"{rx:>9}" + "".join(cells))

    print("\nper-pair detail (X -> Y):")
    for (rx, cy), (res, n) in results.items():
        print(
            f"  {rx:>9} -> {cy:<9} truth={n:4d} "
            f"TP={res.true_positives:4d} FP={res.false_positives:3d} "
            f"P={res.precision:.3f} R={res.recall:.3f} F1={res.f1:.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
