"""Ground-truth loader for the M3.2 evaluation harness.

Parses a real R8/ProGuard-format `mapping.txt` (original clear name ->
obfuscated name; one class per top-level line, member lines indented and
ignored here) into a `{original_fqcn: obfuscated_fqcn}` dict — the class-level
ground truth a real double build (R8 off vs R8 on) produces for free.

This is the same file format `apkdiff.deobf.render_mapping` emits, so the
parser and the writer are format-compatible by construction.
"""

from __future__ import annotations

from pathlib import Path

_CLASS_LINE_SEP = " -> "

# D8/R8 compiler-generated bookkeeping class markers (lambda desugaring, API
# backport shims, large-constant-pool outlining) — deliberately narrow.
# Plain anonymous-class suffixes like "Foo$bar$1" are NOT matched here: the
# CalculatorM3 diagnosis (M3.2) found these commonly hold real logic (e.g.
# Kotlin coroutine continuation bodies), so excluding them would throw away
# exactly the classes recall improvements should be measured against.
_SYNTHETIC_NAME_MARKERS = ("$$ExternalSynthetic", "$$Lambda$")


def is_synthetic_like(fqcn: str) -> bool:
    """Heuristic: does this donor-side class name look like compiler-generated
    bookkeeping rather than real logic worth grading recall against?
    """
    return any(marker in fqcn for marker in _SYNTHETIC_NAME_MARKERS)


# R8 records classes it *deleted* (dead-code shrinking) in mapping.txt with a
# placeholder obfuscated name like `R8$$REMOVED$$CLASS$$213`. These classes do
# not exist in the APK, so counting them as ground truth is a phantom false
# negative — the matcher can never pair a class that isn't there. Exclude them
# from every oracle (measured at up to 5% of a corpus's app classes).
def is_removed_target(obf_name: str) -> bool:
    return "R8$$REMOVED" in obf_name


def parse_class_mapping(text: str) -> dict[str, str]:
    """Parse class-level lines of a ProGuard mapping.txt.

    Returns `{original_fqcn: obfuscated_fqcn}`. Indented member lines and
    `#`-comments are skipped.
    """
    mapping: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line or raw_line[0].isspace():
            continue  # blank or an indented member line
        line = raw_line.strip()
        if line.startswith("#") or not line.endswith(":"):
            continue
        original, sep, obf = line[:-1].partition(_CLASS_LINE_SEP)
        if not sep:
            continue
        original, obf = original.strip(), obf.strip()
        if original and obf:
            mapping[original] = obf
    return mapping


def load_class_mapping(path: str | Path) -> dict[str, str]:
    return parse_class_mapping(Path(path).read_text())
