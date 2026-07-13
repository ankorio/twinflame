from __future__ import annotations

import sys
from pathlib import Path

from twinflame import components
from twinflame.changes import (
    change_set,
    classify_match,
    filter_class,
    render_csv,
    render_xml,
    select_changes,
    sensitive_summary,
)
from twinflame.model import Match

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402

_ACCESSIBILITY = "Landroid/accessibilityservice/AccessibilityService;"


def _cls(descriptor="La/a;", **kw):
    return syn.make_class(syn.ClassSpec(descriptor=descriptor, source_file="F.kt", **kw))


def test_direct_sensitive_superclass_labeled():
    c = _cls(superclass=_ACCESSIBILITY)
    labels = components.component_labels([c])
    assert labels[c.descriptor] == {"BAS"}


def test_transitive_superclass_resolves_through_app_class():
    # app-local subclass chain: leaf -> Mid -> AccessibilityService
    mid = _cls(descriptor="Lmid;", superclass=_ACCESSIBILITY)
    leaf = _cls(descriptor="Lleaf;", superclass="Lmid;")
    labels = components.component_labels([mid, leaf])
    assert labels["Lleaf;"] == {"BAS"}
    assert labels["Lmid;"] == {"BAS"}


def test_interface_based_component_labeled():
    c = _cls(interfaces=("Landroid/content/BroadcastReceiver;",))
    assert components.component_labels([c])[c.descriptor] == {"RCV"}


def test_ordinary_class_has_no_component():
    assert components.component_labels([_cls(superclass="Ljava/lang/Object;")]) == {}


def test_paired_class_carries_super_interfaces_and_components():
    a = _cls(descriptor="La;", superclass=_ACCESSIBILITY)
    b = _cls(descriptor="Lb;", superclass=_ACCESSIBILITY)
    cmap = components.merge_labels(
        components.component_labels([a]), components.component_labels([b])
    )
    v = classify_match(Match(a, b, 1.0), component_map=cmap)
    assert v.lhs_super == _ACCESSIBILITY and v.rhs_super == _ACCESSIBILITY
    assert v.components == ("BAS",)


def test_sensitive_summary_flags_shared_only():
    a = _cls(descriptor="La;", superclass=_ACCESSIBILITY)
    b = _cls(descriptor="Lb;", superclass=_ACCESSIBILITY)
    plain = Match(_cls(descriptor="Lc;"), _cls(descriptor="Ld;"), 1.0)
    cmap = components.merge_labels(
        components.component_labels([a]), components.component_labels([b])
    )
    changes = change_set([Match(a, b, 1.0), plain], component_map=cmap)
    shared = sensitive_summary(changes)
    assert len(shared) == 1 and shared[0].components == ("BAS",)


def test_select_and_filter_class():
    modified = Match(
        _cls(descriptor="La;", methods=(syn.MethodSpec(name="m", calls=("Landroid/util/Log;->d(Ljava/lang/String;)I",)),)),
        _cls(descriptor="Lb;", methods=(syn.MethodSpec(name="m", calls=("Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;",)),)),
        0.6,
    )
    unchanged = Match(_cls(descriptor="Lc;"), _cls(descriptor="Ld;"), 1.0)
    changes = change_set([modified, unchanged])
    assert {c.kind for c in select_changes(changes, "changed")} == {"modified"}
    assert {c.kind for c in select_changes(changes, "unchanged")} == {"unchanged"}
    assert [c.lhs for c in filter_class(changes, "La;")] == ["La;"]


def test_csv_and_xml_serialize():
    changes = change_set([Match(_cls(descriptor="La;", superclass=_ACCESSIBILITY),
                                _cls(descriptor="Lb;", superclass=_ACCESSIBILITY), 1.0)])
    csv_out = render_csv(changes)
    assert csv_out.splitlines()[0].startswith("kind,origin,library,confidence")
    assert _ACCESSIBILITY in csv_out
    xml_out = render_xml(changes)
    assert '<twinflame schema_version="3">' in xml_out
    assert "<lhs_super>" in xml_out
