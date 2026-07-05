from __future__ import annotations

import sys
from pathlib import Path

from apkdiff.boilerplate import is_boilerplate

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402


def _cls(descriptor="La/a;", interfaces=(), n_methods=1):
    methods = tuple(syn.MethodSpec(name=f"m{i}") for i in range(n_methods))
    return syn.make_class(syn.ClassSpec(descriptor=descriptor, interfaces=interfaces, methods=methods))


def test_tiny_comparator_is_boilerplate():
    c = _cls(interfaces=("Ljava/util/Comparator;",), n_methods=1)
    assert is_boilerplate(c)
    c2 = _cls(interfaces=("Ljava/util/Comparator;",), n_methods=2)  # compare + bridge
    assert is_boilerplate(c2)


def test_comparator_with_real_logic_is_kept():
    # A hand-written comparator with several methods is not a generated twin.
    c = _cls(interfaces=("Ljava/util/Comparator;",), n_methods=5)
    assert not is_boilerplate(c)


def test_non_comparator_is_not_boilerplate():
    assert not is_boilerplate(_cls(interfaces=("Landroid/os/Parcelable;",), n_methods=1))
    assert not is_boilerplate(_cls(interfaces=(), n_methods=1))


def test_boilerplate_signal_survives_obfuscated_name():
    # Descriptor is meaningless (renamed) — detection is by the framework interface.
    c = _cls(descriptor="Lz/z/q;", interfaces=("Ljava/util/Comparator;",), n_methods=1)
    assert is_boilerplate(c)
