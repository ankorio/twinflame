"""Stub for a real DEX/APK emitter (deferred).

The integration tests currently exercise the diff pipeline at the
Python-object layer via `synthetic.py`, which covers every algorithm the
engine implements (cluster, signature, accurate, api). Bringing up a full
binary DEX emitter is tracked separately; until it lands, `loader.py` is
covered by a smoke test only and the end-to-end file-format roundtrip is
not asserted by CI.

When implemented, this module should expose:

    def build_apk(specs: list[ClassSpec], out_path: Path,
                  package: str = "test.synthetic") -> Path: ...

producing a parseable APK ZIP with classes.dex (DEX-035) plus a minimal
binary AXML AndroidManifest.xml.
"""

from __future__ import annotations

from pathlib import Path


def build_apk(specs: list, out_path: Path, package: str = "test.synthetic") -> Path:
    raise NotImplementedError(
        "Binary DEX/APK emission is not yet implemented. "
        "Use tests/fixtures/synthetic.py for in-memory Class fixtures, "
        "or supply a real APK for loader-level tests."
    )
