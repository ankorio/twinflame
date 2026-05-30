from __future__ import annotations

import math

import pytest

from apkdiff.cluster import (
    GLOBAL_POOL_KEY,
    OBFUSCATED_POOL_KEY,
    ClusterOptions,
    build_pools,
    is_obfuscated_package,
    shannon_entropy,
)
from apkdiff.model import AccessFlag, Class


def _cls(pkg: str, name: str = "X") -> Class:
    desc = "L" + (pkg.replace(".", "/") + "/" + name if pkg else name) + ";"
    return Class(
        descriptor=desc,
        package=pkg,
        name=name,
        source_file=None,
        access=AccessFlag.PUBLIC,
        is_inner=False,
        is_synthetic=False,
        is_external=False,
        methods=(),
        fields=(),
        strings=(),
    )


def test_shannon_entropy_zero_on_uniform_string():
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("") == 0.0


def test_shannon_entropy_max_on_uniform_distribution():
    # 4 unique chars, each appearing once → entropy = log2(4) = 2
    assert shannon_entropy("abcd") == pytest.approx(2.0)


def test_shannon_entropy_bounded_by_log2_of_alphabet():
    s = "hello world this is a longer sample"
    alphabet_size = len(set(s))
    assert shannon_entropy(s) <= math.log2(alphabet_size) + 1e-9


def test_obfuscation_detection_flags_short_low_entropy_segments():
    opts = ClusterOptions(find_obfuscated=True)
    assert is_obfuscated_package("a.b.c", opts) is True
    assert is_obfuscated_package("a", opts) is True


def test_obfuscation_detection_passes_normal_package_names():
    opts = ClusterOptions(find_obfuscated=True)
    assert is_obfuscated_package("com.acme.app", opts) is False
    assert is_obfuscated_package("org.example.web.api", opts) is False


def test_obfuscation_detection_requires_all_segments_to_qualify():
    # Clear-text root segments protect the package even when a leaf segment
    # is short — Proguard typically renames types, not vendor packages.
    opts = ClusterOptions(find_obfuscated=True)
    assert is_obfuscated_package("com.acme.a", opts) is False
    # But every-segment-short DOES flag.
    assert is_obfuscated_package("a.b.cd", opts) is True


def test_build_pools_groups_classes_by_package():
    opts = ClusterOptions()
    lhs = [_cls("com.x.a", "A"), _cls("com.x.a", "B"), _cls("com.x.b", "C")]
    rhs = [_cls("com.x.a", "A2"), _cls("com.x.b", "C2")]
    pools = build_pools(lhs, rhs, opts)
    by_key = {p.key: p for p in pools}
    assert set(by_key) == {"com.x.a", "com.x.b"}
    assert len(by_key["com.x.a"].lhs) == 2
    assert len(by_key["com.x.a"].rhs) == 1


def test_build_pools_disabled_yields_one_global_pool():
    opts = ClusterOptions(enable=False)
    lhs = [_cls("com.x.a"), _cls("com.x.b")]
    rhs = [_cls("com.y.a")]
    pools = build_pools(lhs, rhs, opts)
    assert len(pools) == 1
    assert pools[0].key == GLOBAL_POOL_KEY
    assert len(pools[0].lhs) == 2
    assert len(pools[0].rhs) == 1


def test_build_pools_routes_obfuscated_into_fallback_pool():
    opts = ClusterOptions(find_obfuscated=True)
    lhs = [_cls("com.acme", "Clear"), _cls("a.b", "Obf")]
    rhs = [_cls("a.b", "Obf2")]
    pools = build_pools(lhs, rhs, opts)
    keys = {p.key for p in pools}
    assert OBFUSCATED_POOL_KEY in keys
    assert "com.acme" in keys
    obf = next(p for p in pools if p.key == OBFUSCATED_POOL_KEY)
    assert len(obf.lhs) == 1
    assert len(obf.rhs) == 1


def test_build_pools_emits_unmatched_pools_for_added_or_deleted_packages():
    # Packages present in lhs but not rhs (and vice versa) should still
    # produce a pool — that's how downstream code surfaces deleted/added.
    opts = ClusterOptions()
    lhs = [_cls("com.x.only_lhs")]
    rhs = [_cls("com.x.only_rhs")]
    pools = build_pools(lhs, rhs, opts)
    keys = {p.key for p in pools}
    assert keys == {"com.x.only_lhs", "com.x.only_rhs"}


def test_obfuscation_disabled_keeps_short_packages_in_their_own_pool():
    opts = ClusterOptions(find_obfuscated=False)
    lhs = [_cls("a.b.c", "X"), _cls("com.acme", "Y")]
    rhs = []
    pools = build_pools(lhs, rhs, opts)
    keys = {p.key for p in pools}
    assert "a.b.c" in keys
    assert "com.acme" in keys
    assert OBFUSCATED_POOL_KEY not in keys
