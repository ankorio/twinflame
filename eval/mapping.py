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
