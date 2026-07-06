"""Cross-version deobfuscation mapping (--deobfuscation-map)."""

from __future__ import annotations

import synthetic

from twinflame.deobf import build_mapping, recovered_head, render_mapping
from twinflame.model import Match, MethodMatch


def _cls(descriptor, *, source_file=None, fields=()):
    return synthetic.make_class(
        synthetic.ClassSpec(
            descriptor=descriptor,
            source_file=source_file,
            methods=synthetic.standard_methods(),
            fields=fields,
        )
    )


def _match(
    donor_desc,
    target_desc,
    *,
    donor_src=None,
    distance=1.0,
    anchored=False,
    method_matches=(),
    donor_fields=(),
    target_fields=(),
):
    breakdown = {"anchored": 1.0} if anchored else {}
    return Match(
        lhs=_cls(donor_desc, source_file=donor_src, fields=donor_fields),
        rhs=_cls(target_desc, fields=target_fields),
        distance=distance,
        breakdown=breakdown,
        method_matches=method_matches,
    )


def _method(name, descriptor="()V", return_type="V", arg_count=0):
    return synthetic.make_method(
        synthetic.MethodSpec(name=name, descriptor=descriptor, return_type=return_type, arg_count=arg_count)
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


# --- M2.3: method + field member propagation -----------------------------


def test_matched_method_is_recovered_unconditionally():
    donor = _method("getValue", descriptor="()I", return_type="I")
    target = _method("a", descriptor="()I", return_type="I")
    mm = MethodMatch(lhs=donor, rhs=target, score=1.0, status="matched")
    m = _match("La0/a;", "Lx6/q;", donor_src="Foo.java", method_matches=(mm,))

    entries = build_mapping([m])
    assert len(entries[0].methods) == 1
    method = entries[0].methods[0]
    assert method.original_signature == "int getValue()"
    assert method.obfuscated_name == "a"
    assert method.trusted is True


def test_modified_method_gated_by_confidence():
    donor = _method("setValue", descriptor="(I)V", arg_count=1)
    target = _method("b", descriptor="(I)V", arg_count=1)

    low = MethodMatch(lhs=donor, rhs=target, score=0.5, status="modified")
    m_low = _match("La0/a;", "Lx6/q;", donor_src="Foo.java", method_matches=(low,))
    assert build_mapping([m_low], min_confidence=0.8)[0].methods == ()

    high = MethodMatch(lhs=donor, rhs=target, score=0.9, status="modified")
    m_high = _match("La0/a;", "Lx6/q;", donor_src="Foo.java", method_matches=(high,))
    entries = build_mapping([m_high], min_confidence=0.8)
    assert len(entries[0].methods) == 1
    assert entries[0].methods[0].trusted is False  # 0.9 < _TRUSTED (0.95)


def test_added_and_deleted_methods_are_skipped():
    added = MethodMatch(lhs=None, rhs=_method("a"), score=0.0, status="added")
    deleted = MethodMatch(lhs=_method("old"), rhs=None, score=0.0, status="deleted")
    m = _match("La0/a;", "Lx6/q;", donor_src="Foo.java", method_matches=(added, deleted))
    assert build_mapping([m])[0].methods == ()


def test_field_members_paired_by_position_and_type():
    donor_fields = (
        synthetic.FieldSpec(name="value", type_desc="I"),
        synthetic.FieldSpec(name="label", type_desc="Ljava/lang/String;"),
    )
    target_fields = (
        synthetic.FieldSpec(name="a", type_desc="I"),
        synthetic.FieldSpec(name="b", type_desc="Ljava/lang/String;"),
    )
    m = _match(
        "La0/a;", "Lx6/q;", donor_src="Foo.java",
        donor_fields=donor_fields, target_fields=target_fields,
    )
    entries = build_mapping([m])
    fields = {f.original_name: f.obfuscated_name for f in entries[0].fields}
    assert fields == {"value": "a", "label": "b"}
    assert all(f.trusted for f in entries[0].fields)


def test_field_members_skip_type_mismatch_at_position():
    donor_fields = (synthetic.FieldSpec(name="value", type_desc="I"),)
    target_fields = (synthetic.FieldSpec(name="a", type_desc="Ljava/lang/String;"),)
    m = _match(
        "La0/a;", "Lx6/q;", donor_src="Foo.java",
        donor_fields=donor_fields, target_fields=target_fields,
    )
    assert build_mapping([m])[0].fields == ()


def test_method_signature_handles_androguards_space_separated_descriptor():
    # Regression: androguard's Method.descriptor uses its own "pretty" format
    # for multi-arg protos ("(A A A ...)R", space-separated), not the compact
    # raw JVM descriptor — a naive parse mis-split this into an extra empty
    # "argument" between real ones (rendered as a stray ", , ").
    donor = _method("createException", descriptor="(I Ljava/lang/String;)V", arg_count=2)
    target = _method("a", descriptor="(I Ljava/lang/String;)V", arg_count=2)
    mm = MethodMatch(lhs=donor, rhs=target, score=1.0, status="matched")
    m = _match("La0/a;", "Lx6/q;", donor_src="Foo.java", method_matches=(mm,))

    signature = build_mapping([m])[0].methods[0].original_signature
    # No space after the comma: real ProGuard mapping.txt is whitespace-
    # column-separated, so "int, java.lang.String" would misparse.
    assert signature == "void createException(int,java.lang.String)"
    assert "  " not in signature
    assert ", " not in signature


def test_render_mapping_emits_indented_member_lines():
    donor = _method("getValue", descriptor="()I", return_type="I")
    target = _method("a", descriptor="()I", return_type="I")
    mm = MethodMatch(lhs=donor, rhs=target, score=1.0, status="matched")
    donor_fields = (synthetic.FieldSpec(name="value", type_desc="I"),)
    target_fields = (synthetic.FieldSpec(name="b", type_desc="I"),)
    m = _match(
        "La0/a;", "Lx6/q;", donor_src="Foo.java",
        method_matches=(mm,), donor_fields=donor_fields, target_fields=target_fields,
    )
    text = render_mapping(build_mapping([m]))
    assert "x6.Foo -> x6.q:\n" in text
    assert "    int getValue() -> a\n" in text
    assert "    int value -> b\n" in text
