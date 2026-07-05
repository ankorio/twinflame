from __future__ import annotations

import json
import sys
from pathlib import Path

from apkdiff.changes import (
    change_set,
    classify_match,
    counts,
    render_json,
    render_text,
)
from apkdiff.model import Match, MethodMatch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402


def _cls(descriptor="La/a;", src="Foo.kt", **kw):
    return syn.make_class(syn.ClassSpec(descriptor=descriptor, source_file=src, **kw))


def _m_call(target):
    return syn.MethodSpec(name="m", calls=(target,))


def test_added_and_removed():
    added = classify_match(Match(None, _cls(methods=(syn.MethodSpec(name="m", instr_count=9),)), 0.0))
    removed = classify_match(Match(_cls(methods=(syn.MethodSpec(name="m", instr_count=4),)), None, 0.0))
    assert added.kind == "added" and added.rhs is not None and added.magnitude == 9
    assert removed.kind == "removed" and removed.lhs is not None and removed.magnitude == 4


def test_modified_when_semantic_features_differ():
    a = _cls(methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),))
    b = _cls(descriptor="Lx/y;", methods=(_m_call("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"),))
    v = classify_match(Match(a, b, 0.7))
    assert v.kind == "modified"
    assert v.magnitude == 2  # one call added, one removed
    assert v.delta is not None
    assert "Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;" in v.delta.calls_added


def test_cosmetic_when_only_structure_differs():
    # Same semantic features, structural distance < 1 => re-obfuscation noise.
    kw = dict(methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),), strings=("tag123",))
    a = _cls(descriptor="La/a;", **kw)
    b = _cls(descriptor="Lz/z;", **kw)
    v = classify_match(Match(a, b, 0.83))
    assert v.kind == "cosmetic"
    assert v.magnitude == 0 and v.delta is None


def test_unchanged_when_identical():
    kw = dict(methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),))
    v = classify_match(Match(_cls(**kw), _cls(**kw), 1.0))
    assert v.kind == "unchanged"


def test_anchored_flag_carried():
    a = _cls(methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),))
    b = _cls(descriptor="Lx/y;", methods=(_m_call("Landroid/os/Bundle;-><init>()V"),))
    v = classify_match(Match(a, b, 0.4, {"anchored": 1.0}))
    assert v.anchored is True and v.kind == "modified"


def test_change_set_ranks_modified_first_cosmetic_last():
    big = Match(
        _cls(descriptor="La;", methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),)),
        _cls(descriptor="Lb;", methods=(
            _m_call("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"),
            syn.MethodSpec(name="n", calls=("Landroid/os/Bundle;-><init>()V",)),
        )),
        0.5,
    )
    cosmetic = Match(_cls(descriptor="Lc;"), _cls(descriptor="Ld;"), 0.82)
    added = Match(None, _cls(descriptor="Le;", methods=(syn.MethodSpec(name="m", instr_count=3),)), 0.0)
    ordered = change_set([cosmetic, added, big])
    kinds = [c.kind for c in ordered]
    assert kinds[0] == "modified"          # highest review-worthiness first
    assert kinds.index("added") < kinds.index("cosmetic")  # added ranks above cosmetic
    assert kinds[-1] == "cosmetic"


def test_render_json_and_text_shape():
    a = _cls(methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),))
    b = _cls(descriptor="Lx/y;", methods=(_m_call("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"),))
    cs = change_set([Match(a, b, 0.7)])
    doc = json.loads(render_json(cs))
    assert doc["summary"]["modified"] == 1
    assert doc["changes"][0]["kind"] == "modified"
    assert "calls_added" in doc["changes"][0]["delta"]
    txt = render_text(cs)
    assert "change summary:" in txt and "modified=1" in txt


def test_app_type_change_threads_through_class_map_and_json():
    # La/a; depends on app class La/h1;; its match Lx/y; depends on Lx/h2; instead.
    # The map (built from the sibling matches) translates the owners, so the
    # changed app-type dependency is flagged on the La/a; -> Lx/y; row.
    a = _cls(descriptor="La/a;", fields=(syn.FieldSpec(name="f", type_desc="La/h1;"),))
    b = _cls(descriptor="Lx/y;", fields=(syn.FieldSpec(name="f", type_desc="Lx/h2;"),))
    matches = [
        Match(a, b, 0.9),
        Match(_cls(descriptor="La/h1;"), _cls(descriptor="Lx/h1;"), 1.0),
        Match(_cls(descriptor="La/h2;"), _cls(descriptor="Lx/h2;"), 1.0),
    ]
    doc = json.loads(render_json(change_set(matches)))
    row = next(r for r in doc["changes"] if r["lhs"] == "La/a;")
    assert row["kind"] == "modified"
    assert row["delta"]["app_types_added"] == ["Lx/h2;"]
    assert row["delta"]["app_types_removed"] == ["Lx/h1;"]


def test_app_changes_rank_above_larger_library_changes():
    # A small app-package edit must outrank a bigger library edit of the same kind.
    app = Match(
        _cls(descriptor="Lcom/myapp/A;", methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),)),
        _cls(descriptor="Lcom/myapp/A;", methods=(_m_call("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"),)),
        0.7,
    )
    lib = Match(
        _cls(descriptor="Landroidx/work/W;", methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),)),
        _cls(descriptor="Landroidx/work/W;", methods=(
            _m_call("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"),
            syn.MethodSpec(name="n", calls=("Landroid/os/Bundle;-><init>()V", "Landroid/os/Handler;-><init>()V")),
        )),
        0.6,
    )
    ordered = change_set([lib, app], dev_package="com.myapp")
    kinds = [c.kind for c in ordered]
    assert kinds[0] == "modified" and kinds[1] == "modified"
    assert ordered[0].origin == "app"      # smaller app edit first
    assert ordered[1].origin == "library"  # bigger library edit demoted
    assert ordered[0].magnitude < ordered[1].magnitude


def test_origin_defaults_to_library_or_unknown_without_dev_package():
    lib = Match(_cls(descriptor="Landroidx/work/W;"), _cls(descriptor="Landroidx/work/W;"), 1.0)
    app_like = Match(_cls(descriptor="Lcom/myapp/A;"), _cls(descriptor="Lcom/myapp/A;"), 1.0)
    ordered = change_set([lib, app_like])  # no dev_package
    by_lhs = {c.lhs: c.origin for c in ordered}
    assert by_lhs["Landroidx/work/W;"] == "library"
    assert by_lhs["Lcom/myapp/A;"] == "unknown"  # can't confirm app without dev pkg


def test_method_localization_flows_into_report():
    a = _cls(descriptor="La/a;", methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),))
    b = _cls(descriptor="Lx/y;", methods=(_m_call("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"),))
    am = syn.make_method(syn.MethodSpec(name="run", descriptor="()V",
                                        calls=("Landroid/util/Log;->d(Ljava/lang/String;)I",), instr_count=6))
    bm = syn.make_method(syn.MethodSpec(name="run", descriptor="()V",
                                        calls=("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;",), instr_count=6))
    m = Match(a, b, 0.6, {}, (MethodMatch(lhs=am, rhs=bm, score=0.6, status="modified"),))
    doc = json.loads(render_json(change_set([m])))
    row = doc["changes"][0]
    assert row["kind"] == "modified"
    assert "methods" in row and row["methods"][0]["status"] == "modified"
    assert row["methods"][0]["calls_added"] == ["Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"]
    txt = render_text(change_set([m]))
    assert "run()V:" in txt  # method sub-line rendered under the modified class


def test_counts_helper():
    a = _cls(methods=(_m_call("Landroid/util/Log;->d(Ljava/lang/String;)I"),))
    b = _cls(descriptor="Lx/y;", methods=(_m_call("Landroid/os/Bundle;-><init>()V"),))
    cs = change_set([Match(a, b, 0.7), Match(None, _cls(descriptor="Ln;"), 0.0)])
    c = counts(cs)
    assert c["modified"] == 1 and c["added"] == 1
