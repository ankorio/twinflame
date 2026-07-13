"""libdict + library_map threading: the known-library dictionary integration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from twinflame import libdict
from twinflame.changes import change_set, classify_match, render_csv, render_json
from twinflame.model import Match
from twinflame.provenance import dev_descriptor_prefixes
from twinflame.score import family_classes
from twinflame.signature import compute_signature

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402

libsigs = pytest.importorskip(
    "twinflame_libsigs", reason="twinflame_libsigs not installed (companion project)")
from twinflame_libsigs.detect import LibraryDetector  # noqa: E402


def _cls(descriptor, *, calls=(), strings=(), instr=25):
    return syn.make_class(syn.ClassSpec(
        descriptor=descriptor, source_file=None,
        methods=(syn.MethodSpec(name="m", instr_count=instr, calls=tuple(calls)),),
        strings=tuple(strings)))


def _detector_for(classes, coord="com.squareup.okhttp3:okhttp", radius=4):
    """A detector whose dictionary is exactly `classes` (payload metadata via a
    fake sidecar, as `from_pack` would attach)."""
    entries = [(i, compute_signature(c).combined, c.strings)
               for i, c in enumerate(classes)]
    det = LibraryDetector.build(entries, radius=radius)
    det._payloads = {str(i): {"coord": coord, "ranges": ["4.0.1..4.9.3"], "fqcn": "x"}
                     for i in range(len(classes))}
    return det


# ---- label_classes ---------------------------------------------------------

def test_label_classes_labels_recognised_classes():
    lib = [_cls(f"La/b{i};", calls=(f"Landroid/x/C{i};->m()V",)) for i in range(6)]
    det = _detector_for(lib)
    labels = libdict.label_classes(lib, det, min_classes=5)
    assert len(labels) == 6
    assert labels["La/b0;"] == "com.squareup.okhttp3:okhttp@4.0.1..4.9.3"


def test_label_classes_aggregation_drops_sparse_libraries():
    # Only 2 classes hit the dictionary -> below min_classes=5 -> not believed.
    lib = [_cls(f"La/b{i};", calls=(f"Landroid/x/C{i};->m()V",)) for i in range(2)]
    det = _detector_for(lib)
    assert libdict.label_classes(lib, det, min_classes=5) == {}
    assert len(libdict.label_classes(lib, det, min_classes=2)) == 2


def test_label_classes_skips_trivial_classes():
    # Below the min-instr precision floor the class is never probed.
    tiny = [_cls(f"La/t{i};", instr=3) for i in range(6)]
    det = _detector_for(tiny)
    assert libdict.label_classes(tiny, det, min_classes=1) == {}


# ---- find_pack -------------------------------------------------------------

def test_find_pack_resolution(tmp_path, monkeypatch):
    pack = tmp_path / "p.tflp"
    pack.write_bytes(b"x")
    monkeypatch.delenv(libdict.ENV_PACK, raising=False)
    assert libdict.find_pack(str(pack)) == pack
    assert libdict.find_pack(str(tmp_path / "missing.tflp")) is None
    monkeypatch.setenv(libdict.ENV_PACK, str(pack))
    assert libdict.find_pack(None) == pack


# ---- change_set threading ---------------------------------------------------

def test_library_map_demotes_renamed_class_and_carries_label():
    label = "com.squareup.okhttp3:okhttp@4.0.1..4.9.3"
    a = _cls("La/a;")   # renamed: no prefix heuristic can see it
    b = _cls("Lb/b;")
    v = classify_match(Match(a, b, 1.0), library_map={"La/a;": label})
    assert v.origin == "library"
    assert v.library == label
    # without the map the same class is "unknown"
    assert classify_match(Match(a, b, 1.0)).origin == "unknown"


def test_dev_prefix_beats_dictionary_evidence():
    # An app vendoring a library under its own package still reviews as app.
    a = _cls("Lcom/acme/app/Vendored;")
    v = classify_match(
        Match(a, a, 1.0),
        dev_prefix=dev_descriptor_prefixes("com.acme.app"),
        library_map={"Lcom/acme/app/Vendored;": "x:y@1"})
    assert v.origin == "app"
    assert v.library == "x:y@1"  # evidence still recorded


def test_library_map_applies_to_added_and_removed():
    added = classify_match(Match(None, _cls("Lq/q;"), 0.0),
                           library_map={"Lq/q;": "g:a@1..2"})
    removed = classify_match(Match(_cls("Lq/q;"), None, 0.0),
                             library_map={"Lq/q;": "g:a@1..2"})
    assert added.origin == removed.origin == "library"
    assert added.library == removed.library == "g:a@1..2"


def test_change_set_ranks_dictionary_library_below_unknown():
    lib_match = Match(_cls("La/a;"), _cls("Lb/b;"), 0.5)
    app_match = Match(_cls("Lc/c;", strings=("s1",)), _cls("Ld/d;", strings=("s2",)), 0.5)
    ordered = change_set([lib_match, app_match], library_map={"La/a;": "g:a@1"})
    assert [c.origin for c in ordered] == ["unknown", "library"]


def test_renderers_include_library_field():
    label = "g:a@1..2"
    changes = change_set([Match(_cls("La/a;"), _cls("Lb/b;"), 1.0)],
                         library_map={"La/a;": label})
    assert f'"library": "{label}"' in render_json(changes)
    assert label in render_csv(changes)


# ---- score-side exclusion ---------------------------------------------------

def test_family_classes_excludes_dictionary_labeled():
    fam = [_cls("La/a;"), _cls("Lb/b;")]
    kept = family_classes(fam, library_descriptors=frozenset({"La/a;"}))
    assert [c.descriptor for c in kept] == ["Lb/b;"]
