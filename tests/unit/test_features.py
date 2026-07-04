from __future__ import annotations

import sys
from pathlib import Path

from apkdiff.features import (
    app_type_refs,
    class_change,
    class_features,
    diff_features,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402


def _cls(descriptor="La/a;", **kw):
    return syn.make_class(syn.ClassSpec(descriptor=descriptor, **kw))


def test_extracts_framework_calls_and_filters_app_calls():
    c = _cls(
        methods=(
            syn.MethodSpec(
                name="m",
                calls=(
                    "Landroid/util/Log;->d(Ljava/lang/String;)I",  # framework
                    "La/b;->helper()V",                            # app -> filtered
                ),
            ),
        ),
    )
    f = class_features(c)
    assert f.fw_calls == frozenset({"Landroid/util/Log;->d(Ljava/lang/String;)I"})


def test_extracts_strings_and_framework_type_refs():
    c = _cls(
        superclass="Landroid/app/Activity;",
        interfaces=("Landroid/os/Parcelable;", "Lapp/Own;"),  # app iface filtered
        fields=(
            syn.FieldSpec(name="f", type_desc="Ljava/lang/String;"),
            syn.FieldSpec(name="g", type_desc="Lapp/Own;"),  # app field type filtered
        ),
        strings=("hello", "world", " [", ""),  # trivial strings dropped
    )
    f = class_features(c)
    assert f.strings == frozenset({"hello", "world"})
    assert "Landroid/app/Activity;" in f.fw_refs
    assert "Landroid/os/Parcelable;" in f.fw_refs
    assert "Ljava/lang/String;" in f.fw_refs
    assert "Lapp/Own;" not in f.fw_refs  # app types are name-unstable across builds


def test_identical_semantic_content_is_not_a_semantic_change():
    # Same framework calls / strings / refs, even if structure differed, is
    # cosmetic (re-obfuscation noise), not a semantic change.
    kw = dict(
        superclass="Landroid/app/Activity;",
        methods=(syn.MethodSpec(name="m", calls=("Landroid/util/Log;->d(Ljava/lang/String;)I",)),),
        strings=("tag",),
    )
    a = _cls(descriptor="La/a;", **kw)
    b = _cls(descriptor="Lx/y;", **kw)  # different obfuscated name, same semantics
    delta = class_change(a, b)
    assert not delta.is_semantic
    assert delta.magnitude == 0
    assert delta.summary() == "no semantic change"


def test_detects_added_and_removed_calls_strings_types():
    a = _cls(
        methods=(syn.MethodSpec(name="m", calls=("Landroid/util/Log;->d(Ljava/lang/String;)I",)),),
        strings=("old",),
        superclass="Landroid/app/Activity;",
    )
    b = _cls(
        methods=(syn.MethodSpec(
            name="m",
            calls=("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;",),
        ),),
        strings=("new",),
        superclass="Landroidx/appcompat/app/AppCompatActivity;",
    )
    d = class_change(a, b)
    assert d.is_semantic
    assert d.calls_added == ("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;",)
    assert d.calls_removed == ("Landroid/util/Log;->d(Ljava/lang/String;)I",)
    assert d.strings_added == ("new",) and d.strings_removed == ("old",)
    assert "Landroidx/appcompat/app/AppCompatActivity;" in d.refs_added
    assert "Landroid/app/Activity;" in d.refs_removed
    # magnitude counts every added/removed element
    assert d.magnitude == 6


def test_delta_is_symmetric_reversed():
    a = _cls(strings=("alpha",))
    b = _cls(strings=("beta",))
    fa, fb = class_features(a), class_features(b)
    ab, ba = diff_features(fa, fb), diff_features(fb, fa)
    assert ab.strings_added == ba.strings_removed
    assert ab.strings_removed == ba.strings_added


# --- app-type reference threading (class-match map) -------------------------

def test_app_type_refs_extracted_and_kept_out_of_fw_refs():
    c = _cls(
        superclass="La/base;",  # app superclass
        fields=(syn.FieldSpec(name="f", type_desc="La/helper;"),),  # app field type
    )
    f = class_features(c)
    assert app_type_refs(c) == {"La/base;", "La/helper;"}
    assert f.app_refs == frozenset({"La/base;", "La/helper;"})
    assert "La/base;" not in f.fw_refs  # app types stay out of the fw set


def test_app_refs_ignored_without_a_class_map():
    # Different app superclass names, but no map => not comparable => no delta.
    a = _cls(descriptor="La/a;", superclass="La/base;")
    b = _cls(descriptor="Lx/y;", superclass="Lx/other;")
    d = class_change(a, b)  # no map
    assert d.app_refs_added == () and d.app_refs_removed == ()
    assert not d.is_semantic


def test_app_dependency_unchanged_across_rename_is_not_a_change():
    # Same app superclass, renamed A->B; map translates it => no delta.
    a = _cls(descriptor="La/a;", superclass="La/base;")
    b = _cls(descriptor="Lx/y;", superclass="Lx/base;")
    cmap = {"La/a;": "Lx/y;", "La/base;": "Lx/base;"}
    d = class_change(a, b, cmap)
    assert not d.is_semantic
    assert d.app_refs_added == () and d.app_refs_removed == ()


def test_app_dependency_changed_is_flagged_after_translation():
    a = _cls(descriptor="La/a;", fields=(syn.FieldSpec(name="f", type_desc="La/helper1;"),))
    b = _cls(descriptor="Lx/y;", fields=(syn.FieldSpec(name="f", type_desc="Lx/helper2;"),))
    cmap = {
        "La/a;": "Lx/y;",
        "La/helper1;": "Lx/helper1;",  # helper1 maps to a B-class the RHS no longer uses
        "La/helper2;": "Lx/helper2;",
    }
    d = class_change(a, b, cmap)
    assert d.is_semantic
    assert d.app_refs_added == ("Lx/helper2;",)
    assert d.app_refs_removed == ("Lx/helper1;",)
    assert d.magnitude == 2


def test_untranslatable_app_ref_is_excluded_not_spurious_change():
    # LHS references an app class the matcher did not pair (absent from the map).
    # It must be dropped, not reported as a removed dependency.
    a = _cls(descriptor="La/a;", superclass="La/unmatched;")
    b = _cls(descriptor="Lx/y;")  # no app supertype
    cmap = {"La/a;": "Lx/y;"}  # La/unmatched; deliberately absent
    d = class_change(a, b, cmap)
    assert d.app_refs_removed == () and d.app_refs_added == ()
    assert not d.is_semantic
