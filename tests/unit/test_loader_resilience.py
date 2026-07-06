"""Loader robustness for dumped/partial DEX.

Memory-dumped DEX (a) carry hidden-API flag values androguard's enum rejects and
(b) may include a few genuinely corrupt blobs. Neither should abort the load: we
patch the enum to be lenient and skip unparseable blobs, failing only if nothing
parses. These tests exercise that logic with mocked DEX parsing — no binaries.
"""

from __future__ import annotations

import pytest

from twinflame import loader
from twinflame.loader import LoadError, _parse_dexes, _patch_dex_leniency


def test_leniency_patch_tolerates_unknown_hidden_api_flags():
    _patch_dex_leniency()
    from androguard.core.dex import HiddenApiClassDataItem

    # Values outside the modeled enum (seen in real dumps: 4, 6) must resolve to
    # the zero member instead of raising ValueError.
    for name in ("DomapiApiFlag", "RestrictionApiFlag"):
        enum_cls = getattr(HiddenApiClassDataItem, name, None)
        if enum_cls is None:
            continue
        assert enum_cls(999) is list(enum_cls)[0]


def test_parse_dexes_skips_unparseable_and_keeps_the_rest(monkeypatch, capsys):
    import androguard.core.dex as adx

    def fake_dex(blob):
        if blob == b"bad":
            raise ValueError("corrupt section")
        return f"DEX<{blob.decode()}>"

    monkeypatch.setattr(adx, "DEX", fake_dex)
    out = _parse_dexes([b"good1", b"bad", b"good2", b"bad"])
    assert out == ["DEX<good1>", "DEX<good2>"]
    assert "skipped 2/4" in capsys.readouterr().err


def test_parse_dexes_raises_when_nothing_parses(monkeypatch):
    import androguard.core.dex as adx

    def always_fail(blob):
        raise ValueError("corrupt")

    monkeypatch.setattr(adx, "DEX", always_fail)
    with pytest.raises(LoadError, match="no parseable DEX"):
        _parse_dexes([b"x", b"y"])
