"""Score apkdiff's class matches against a real R8 mapping.txt.

Usage:
    python -m eval.cli donor.apk target.apk mapping.txt [--package PREFIX]

`donor.apk` = the R8-off build (keeps clear names); `target.apk` = the R8-on
build (obfuscated); `mapping.txt` = the real mapping R8 wrote for the target
build. This is the "renaming-only" grading slice from the dev plan's M3.2 —
no inlining/outlining corpus yet (a difficulty-graded follow-up once this
lands, per M3.1's gate on this harness).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apkdiff import api, load

from .mapping import load_class_mapping
from .score import score_class_matches


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval.cli")
    p.add_argument("donor", type=Path, help="R8-off APK (keeps clear names)")
    p.add_argument("target", type=Path, help="R8-on APK (obfuscated)")
    p.add_argument("mapping", type=Path, help="real R8 mapping.txt for the target build")
    p.add_argument("--package", metavar="PREFIX", help="restrict to classes under this package prefix")
    p.add_argument("--threshold", type=float, default=0.8)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    ground_truth = load_class_mapping(args.mapping)

    donor_app = load(args.donor)
    target_app = load(args.target)
    condition = {"package_filtering": args.package} if args.package else None
    donor_classes = api.filter(donor_app.classes, condition)
    target_classes = api.filter(target_app.classes, condition)

    matches = api.diff(donor_classes, target_classes, args.threshold, {})
    result = score_class_matches(matches, ground_truth)

    print(f"ground truth: {len(ground_truth)} classes", file=sys.stderr)
    print(f"true positives:  {result.true_positives}")
    print(f"false positives: {result.false_positives}")
    print(f"false negatives: {result.false_negatives}")
    print(f"precision: {result.precision:.4f}")
    print(f"recall:    {result.recall:.4f}")
    print(f"f1:        {result.f1:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
