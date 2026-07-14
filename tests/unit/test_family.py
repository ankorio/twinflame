"""Malware family signatures: build clustering/coverage + match roundtrip."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402

from twinflame.signature import compute_signature  # noqa: E402

libsigs = pytest.importorskip(
    "twinflame_libsigs", reason="twinflame_libsigs not installed (companion project)")

from twinflame import family as fam  # noqa: E402
from twinflame_libsigs.detect import LibraryDetector  # noqa: E402


_RET = ["V", "I", "J", "F", "Ljava/lang/String;", "Z", "[I", "D"]


def _cls(descriptor, *, seed=0, strings=(), instr=25, source_file=None, nmeth=4):
    """A class whose *structural* features (arg counts, return types, field
    types, framework calls) are driven by `seed`. The signature encodes shape,
    not raw bytecode or app-internal names, so distinct seeds land >20 bits
    apart while the same seed reproduces the same signature exactly."""
    methods = []
    for j in range(nmeth):
        v = seed * 7 + j * 3
        methods.append(syn.MethodSpec(
            name=f"m{j}", arg_count=v % 4, return_type=_RET[v % len(_RET)],
            instr_count=instr + (v % 20), xref_count=v % 5,
            calls=(f"Landroid/os/C{v % 6};->go{v % 3}()V",
                   f"Ljava/util/L{seed % 5};->q()I")))
    fields = tuple(syn.FieldSpec(name=f"f{k}", type_desc=_RET[(seed + k) % len(_RET)])
                   for k in range(seed % 4))
    return syn.make_class(syn.ClassSpec(
        descriptor=descriptor, source_file=source_file,
        methods=tuple(methods), fields=fields, strings=tuple(strings)))


def _sample(sid, classes):
    return fam.FamilySample(sample_id=sid, label=f"s{sid}", digest=f"d{sid}",
                            classes=tuple(classes))


def _detector_for(classes, radius=4):
    entries = [(i, compute_signature(c).combined, c.strings)
               for i, c in enumerate(classes)]
    det = LibraryDetector.build(entries, radius=radius)
    det._payloads = {str(i): {"coord": "c:c", "ranges": ["1.0"], "fqcn": "x"}
                     for i in range(len(classes))}
    return det


# ---- filter_sample_classes --------------------------------------------------

def test_filter_dedups_within_sample_by_signature():
    # Same descriptor structure twice -> one exact signature -> counted once.
    c1 = _cls("La/A;", seed=1)
    c2 = _cls("La/B;", seed=1)   # different name, identical body => same sig
    kept, n_lib = fam.filter_sample_classes([c1, c2], min_instructions=5)
    assert len(kept) == 1 and n_lib == 0


def test_filter_drops_tiny_classes():
    tiny = _cls("La/A;", seed=0, instr=1, nmeth=1)  # ~3 total instructions
    assert tiny.total_instructions < 20
    kept, _ = fam.filter_sample_classes([tiny], min_instructions=20)
    assert kept == []


def test_filter_excludes_library_classes():
    lib = [_cls(f"Lp/L{i};", seed=10 + i) for i in range(6)]
    detector = _detector_for(lib)
    # The same library classes appearing in a sample get dropped by the dict.
    kept, n_lib = fam.filter_sample_classes(lib, detector=detector,
                                            min_instructions=5)
    assert kept == [] and n_lib == 6


# ---- build_family -----------------------------------------------------------

def test_fixture_seeds_are_mutually_far():
    # Guards the fixtures: distinct seeds must sit outside the match radius (12)
    # or the coverage/containment assertions below would be meaningless.
    import itertools
    sigs = [compute_signature(_cls("La/A;", seed=s)).combined for s in range(10)]
    dists = [(a ^ b).bit_count() for a, b in itertools.combinations(sigs, 2)]
    assert min(dists) > 12


def _three_samples_sharing_two_classes():
    shared_a = lambda: _cls("La/Shared1;", seed=1)   # noqa: E731
    shared_b = lambda: _cls("La/Shared2;", seed=2)
    s0 = _sample(0, [shared_a(), shared_b(), _cls("La/Only0;", seed=3)])
    s1 = _sample(1, [shared_a(), shared_b(), _cls("La/Only1;", seed=4)])
    s2 = _sample(2, [shared_a(), _cls("La/Only2;", seed=5)])  # missing shared_b
    return [s0, s1, s2]


def test_build_coverage_counts_distinct_samples():
    f = fam.build_family(_three_samples_sharing_two_classes(), name="fam", k=1)
    cov = {e.fqcn: e.coverage for e in f.entries}
    assert cov["a.Shared1"] == 3     # in all three
    assert cov["a.Shared2"] == 2     # only s0, s1
    assert cov["a.Only0"] == 1


def test_build_k_of_n_filters_by_coverage():
    samples = _three_samples_sharing_two_classes()
    full = fam.build_family(samples, name="fam", k=3)
    assert {e.fqcn for e in full.entries} == {"a.Shared1"}
    k2 = fam.build_family(samples, name="fam", k=2)
    assert {e.fqcn for e in k2.entries} == {"a.Shared1", "a.Shared2"}


def test_build_default_k_is_n():
    f = fam.build_family(_three_samples_sharing_two_classes(), name="fam")
    assert f.k == 3
    assert {e.fqcn for e in f.entries} == {"a.Shared1"}


def test_build_string_intersection():
    a = _cls("La/S;", seed=1, strings=("common_marker", "only_in_a"))
    b = _cls("La/S;", seed=1, strings=("common_marker", "only_in_b"))
    f = fam.build_family([_sample(0, [a]), _sample(1, [b])], name="fam", k=2)
    (entry,) = f.entries
    assert entry.strings == ("common_marker",)


def test_build_entries_sorted_deterministically():
    f = fam.build_family(_three_samples_sharing_two_classes(), name="fam", k=1)
    keys = [(-e.coverage, -e.instructions, e.signature) for e in f.entries]
    assert keys == sorted(keys)


def test_build_rejects_bad_inputs():
    one = [_sample(0, [_cls("La/A;", seed=1)])]
    with pytest.raises(ValueError):
        fam.build_family(one, name="fam")           # < 2 samples
    with pytest.raises(ValueError):
        fam.build_family(_three_samples_sharing_two_classes(), name="fam", k=4)


# ---- build -> save -> load -> match roundtrip -------------------------------

def test_roundtrip_match_is_exact(tmp_path):
    samples = _three_samples_sharing_two_classes()
    f = fam.build_family(samples, name="fam", k=1)
    pack = tmp_path / "fam.tflp"
    fam.save_family(f, pack)

    detector, meta, payloads = fam.load_family_detector(pack, radius=12)
    assert meta["kind"] == "family" and meta["family"] == "fam"

    # A candidate holding every family class (the union of all samples) is fully
    # contained -> score 1.0, all via the exact tier.
    superset = [c for s in samples for c in s.classes]
    whole = fam.match_family(detector, meta, payloads, superset,
                             probe_min_instructions=5)
    assert whole.score == pytest.approx(1.0)
    assert whole.present == whole.total == len(f.entries)
    assert whole.by_tier.get(1, 0) == len(f.entries)

    # Sample 0 contains exactly the entries it contributed (its 3 of the 5).
    part = fam.match_family(detector, meta, payloads, samples[0].classes,
                            probe_min_instructions=5)
    assert part.present == sum(1 for e in f.entries if 0 in e.sample_ids) == 3
    assert 0.0 < part.score < 1.0


def test_roundtrip_disjoint_candidate_scores_zero(tmp_path):
    f = fam.build_family(_three_samples_sharing_two_classes(), name="fam", k=1)
    pack = tmp_path / "fam.tflp"
    fam.save_family(f, pack)
    detector, meta, payloads = fam.load_family_detector(pack, radius=12)

    stranger = [_cls(f"Lz/Z{i};", seed=6 + i) for i in range(4)]  # far from 1..5
    result = fam.match_family(detector, meta, payloads, stranger,
                              probe_min_instructions=5)
    assert result.score == 0.0 and result.present == 0


def test_match_no_strings_ignores_string_tier(tmp_path):
    # A family entry with strings; a candidate that only matches by string.
    a = _cls("La/S;", seed=1, strings=("unique_family_marker_string",))
    b = _cls("La/S;", seed=1, strings=("unique_family_marker_string",))
    f = fam.build_family([_sample(0, [a]), _sample(1, [b])], name="fam", k=2)
    pack = tmp_path / "fam.tflp"
    fam.save_family(f, pack)
    detector, meta, payloads = fam.load_family_detector(pack, radius=12)

    # Structurally unrelated class (seed 7, far from the family's seed 1),
    # same marker string.
    far = _cls("Lq/Q;", seed=7, strings=("unique_family_marker_string",))
    with_str = fam.match_family(detector, meta, payloads, [far],
                                probe_min_instructions=5)
    without = fam.match_family(detector, meta, payloads, [far],
                               probe_min_instructions=5, use_strings=False)
    assert with_str.present == 1 and with_str.by_tier.get(3, 0) == 1
    assert without.present == 0


def test_load_family_detector_rejects_stale_stamp(tmp_path):
    from twinflame_libsigs.pack import write_pack
    from twinflame_libsigs.store import StaleStoreError

    pack = tmp_path / "stale.tflp"
    write_pack(pack, [(0, 123, ("s",))], {"0": {"fqcn": "x"}},
               sig_stamp="deadbeef", meta={"kind": "family"})
    with pytest.raises(StaleStoreError):
        fam.load_family_detector(pack, radius=12)
