"""Cross-version deobfuscation mapping (--deobfuscation-map)."""

from __future__ import annotations

import synthetic

from apkdiff.deobf import build_mapping, recovered_head, render_mapping
from apkdiff.model import Match


def _cls(descriptor, *, source_file=None):
    return synthetic.make_class(
        synthetic.ClassSpec(
            descriptor=descriptor,
            source_file=source_file,
            methods=synthetic.standard_methods(),
        )
    )


def _match(donor_desc, target_desc, *, donor_src=None, distance=1.0, anchored=False):
    breakdown = {"anchored": 1.0} if anchored else {}
    return Match(
        lhs=_cls(donor_desc, source_file=donor_src),
        rhs=_cls(target_desc),
        distance=distance,
        breakdown=breakdown,
    )


def test_recovered_head_from_source_file():
    assert recovered_head(_cls("La0/a;", source_file="ContextCompat.java")) == "ContextCompat"
    assert recovered_head(_cls("Lp4/b;", source_file="Timber.kt")) == "Timber"


def test_recovered_head_falls_back_to_clear_class_name():
    # No source file, but the donor's own name is already meaningful.
    assert recovered_head(_cls("Lcom/acme/LoginManager;")) == "LoginManager"


def test_recovered_head_none_when_nothing_useful():
    # Obfuscated name + stripped source file → nothing to recover.
    assert recovered_head(_cls("La0/a;", source_file="SourceFile")) is None
    assert recovered_head(_cls("La0/a;")) is None


def test_build_mapping_propagates_name_onto_target():
    m = _match("La0/a;", "Lx6/q;", donor_src="ContextCompat.java")
    entries = build_mapping([m])
    assert len(entries) == 1
    # target package kept, simple name recovered
    assert entries[0].original == "x6.ContextCompat"
    assert entries[0].obfuscated == "x6.q"


def test_build_mapping_keeps_inner_structure():
    m = _match("La0/a$c;", "Lx6/q$a;", donor_src="ContextCompat.java")
    entries = build_mapping([m])
    assert entries[0].original == "x6.ContextCompat$a"
    assert entries[0].obfuscated == "x6.q$a"


def test_build_mapping_confidence_gating():
    low = _match("La0/a;", "Lx6/q;", donor_src="Foo.java", distance=0.5)
    assert build_mapping([low], min_confidence=0.8) == []
    # anchored is always included regardless of distance
    anchored = _match("La0/b;", "Lx6/r;", donor_src="Bar.java", distance=0.4, anchored=True)
    assert len(build_mapping([anchored], min_confidence=0.8)) == 1


def test_build_mapping_skips_already_named_targets():
    # Target leaf isn't obfuscated → leave it alone.
    m = _match("La0/a;", "Lcom/acme/MainActivity;", donor_src="Login.java")
    assert build_mapping([m]) == []


def test_build_mapping_disambiguates_collisions():
    # Two distinct target classes in the same package recover the same name.
    m1 = _match("La0/a;", "Lx6/q;", donor_src="Holder.java")
    m2 = _match("La0/b;", "Lx6/r;", donor_src="Holder.java")
    entries = build_mapping([m1, m2])
    originals = sorted(e.original for e in entries)
    # One keeps the clean name, the other is suffixed with its obf leaf.
    assert "x6.Holder" in originals
    assert any(o.startswith("x6.Holder_") for o in originals)
    assert len({e.original for e in entries}) == 2  # all unique


def test_render_mapping_proguard_format_and_low_confidence_flag():
    entries = build_mapping(
        [_match("La0/a;", "Lx6/q;", donor_src="Foo.java", distance=0.85)]
    )
    text = render_mapping(entries)
    assert "x6.Foo -> x6.q:" in text
    assert "# low-confidence (0.85)" in text  # 0.85 < trusted 0.95, not anchored
