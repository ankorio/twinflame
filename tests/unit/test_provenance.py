from __future__ import annotations

import sys
from pathlib import Path

from apkdiff.provenance import (
    classify_origin,
    dev_descriptor_prefix,
    dev_descriptor_prefixes,
    origin_of,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402


def test_dev_prefix_normalisation_accepts_any_form():
    want = "Lcom/acme/wallet/"
    assert dev_descriptor_prefix("com.acme.wallet") == want
    assert dev_descriptor_prefix("com/acme/wallet") == want
    assert dev_descriptor_prefix("Lcom/acme/wallet;") == want
    assert dev_descriptor_prefix(None) is None
    assert dev_descriptor_prefix("") is None


def test_dev_package_wins_over_library_heuristics():
    # A class under the dev package is app even if its name looks library-ish.
    p = dev_descriptor_prefix("com.myapp")
    assert classify_origin("Lcom/myapp/Feature;", "Feature.kt", p) == "app"


def test_maven_coordinate_source_file_is_library():
    # androguard reports the artifact coordinate as SourceFile for AAR classes;
    # a real filename can't contain ':'. Survives package obfuscation.
    assert classify_origin(
        "LH1/a;", "com.google.android.gms:play-services-measurement@@18.0.2", None
    ) == "library"


def test_known_library_prefix_is_library():
    assert classify_origin("Landroidx/work/WorkDatabase;", "WorkDatabase.java", None) == "library"
    assert classify_origin("Lkotlin/coroutines/JobSupport;", "JobSupport.kt", None) == "library"


def test_unknown_when_no_signal():
    assert classify_origin("La/b/c;", "c.java", None) == "unknown"
    # dev package known but class outside it and not obviously a library
    assert classify_origin("La/b/c;", "c.java", dev_descriptor_prefix("com.myapp")) == "unknown"


def test_multi_root_app_prefixes():
    prefixes = dev_descriptor_prefixes(["com.acme", "com.example"])
    assert prefixes == ("Lcom/acme/", "Lcom/example/")
    # both roots classify as app
    assert classify_origin("Lcom/acme/wallet/Svc;", None, prefixes) == "app"
    assert classify_origin("Lcom/example/tmob/Api;", None, prefixes) == "app"
    # a library still sinks
    assert classify_origin("Lcom/fasterxml/jackson/Bean;", None, prefixes) == "library"
    # empty/None means no app labeling, but library heuristics still apply
    assert dev_descriptor_prefixes(None) == ()
    assert classify_origin("Lcom/acme/X;", None, ()) == "unknown"


def test_expanded_library_prefixes():
    assert classify_origin("Lcom/fasterxml/jackson/Bean;", "Bean.java", None) == "library"
    assert classify_origin("Lavro/shaded/Foo;", "Foo.java", None) == "library"


def test_origin_of_uses_class_fields():
    c = syn.make_class(syn.ClassSpec(descriptor="Lcom/myapp/Svc;", source_file="Svc.kt"))
    assert origin_of(c, dev_descriptor_prefix("com.myapp")) == "app"
    assert origin_of(c, None) == "unknown"
