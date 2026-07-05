"""Score apkdiff's class matches against a real R8 mapping.txt.

Usage:
    python -m eval.cli donor.apk target.apk mapping.txt [--package PREFIX]

`donor.apk` = the R8-off build (keeps clear names); `target.apk` = the R8-on
build (obfuscated); `mapping.txt` = the real mapping R8 wrote for the target
build. This is the "renaming-only" grading slice from the dev plan's M3.2 —
no inlining/outlining corpus yet (a difficulty-graded follow-up once this
lands, per M3.1's gate on this harness).

Two consequences of the donor being clear-named while the target is
obfuscated (unlike the main `apkdiff` CLI's symmetric-naming assumption):
- Defaults to a single global pool (`--cluster` opts back into package-based
  pooling) — package clustering only helps when both sides share (or can be
  bucketed into) a common pool key, which doesn't hold here.
- `--package` filters only the donor. Filtering the target by the donor's
  package too would throw away almost every target class R8 actually
  renamed (its package is no longer "com.vagujhelyigergely.foo") and keep
  only the handful R8 happened to leave untouched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apkdiff import api, load

from .mapping import is_removed_target, is_synthetic_like, load_class_mapping
from .score import score_class_matches


def _in_package(fqcn: str, prefix: str) -> bool:
    """Same boundary-safe semantics as api._package_matches, applied to a full FQCN."""
    return fqcn == prefix or fqcn.startswith(prefix + ".")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval.cli")
    p.add_argument("donor", type=Path, help="R8-off APK (keeps clear names)")
    p.add_argument("target", type=Path, help="R8-on APK (obfuscated)")
    p.add_argument("mapping", type=Path, help="real R8 mapping.txt for the target build")
    p.add_argument(
        "--package", metavar="PREFIX",
        help="restrict the donor side to classes under this package prefix. "
        "Only the donor is filtered — the target keeps its full class set, "
        "since its package is renamed/flattened by R8 and we don't know in "
        "advance which obfuscated package the donor's classes ended up in; "
        "that's exactly what matching has to discover.",
    )
    p.add_argument("--threshold", type=float, default=0.8)
    p.add_argument(
        "--cluster",
        action="store_true",
        default=False,
        help="use package-based pooling instead of one global pool (off by "
        "default — the donor keeps its real, clear package while the target's "
        "gets flattened/renamed by R8, so the two never share a pool key; "
        "`find_obfuscated_packages` doesn't bridge this either, since it only "
        "merges packages that look obfuscated on *both* sides into one shared "
        "bucket, and the donor's package never does)",
    )
    p.add_argument(
        "--include-synthetic-like",
        dest="exclude_synthetic_like",
        action="store_false",
        default=True,
        help="grade against compiler-generated bookkeeping classes too "
        "(D8 lambda desugaring / API backport shims, e.g. "
        "'$$ExternalSyntheticLambda...') — excluded by default, since scoring "
        "against them makes recall reflect noise nobody wants to track by "
        "name rather than real code (see M3.2's CalculatorM3 diagnosis)",
    )
    p.add_argument("--assignment", choices=("greedy", "hungarian", "auto"), default="auto")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    ground_truth = load_class_mapping(args.mapping)
    if args.package:
        # The real mapping.txt covers every class R8 processed (the whole app,
        # every library it bundles) — scope it to match --package, or nearly
        # every library class we never even attempted to diff would count as
        # a false negative and tank recall.
        ground_truth = {
            orig: obf for orig, obf in ground_truth.items() if _in_package(orig, args.package)
        }
    if args.exclude_synthetic_like:
        ground_truth = {
            orig: obf for orig, obf in ground_truth.items() if not is_synthetic_like(orig)
        }
    # R8-deleted classes (placeholder `R8$$REMOVED$$CLASS$$…` targets) aren't in
    # the APK — never gradable, always a phantom FN. Drop them from the oracle.
    ground_truth = {
        orig: obf for orig, obf in ground_truth.items() if not is_removed_target(obf)
    }

    donor_app = load(args.donor)
    target_app = load(args.target)
    condition = {"package_filtering": args.package} if args.package else None
    donor_classes = api.filter(donor_app.classes, condition)
    target_classes = list(target_app.classes)  # unfiltered — see module docstring

    matches = api.diff(
        donor_classes, target_classes, args.threshold,
        {"cluster": args.cluster, "assignment": args.assignment},
    )
    result = score_class_matches(matches, ground_truth)

    print(
        f"ground truth: {len(ground_truth)} classes"
        f" (synthetic-like excluded: {args.exclude_synthetic_like})",
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
