from __future__ import annotations

from pathlib import Path

import pytest

from twinflame import normalize


def test_redex_availability_is_a_bool():
    # Just ensure the predicate doesn't crash.
    assert isinstance(normalize.is_available(), bool)


def test_required_passes_include_both_local_dce_and_reg_alloc():
    assert "LocalDcePass" in normalize.REQUIRED_PASSES
    assert "RegAllocPass" in normalize.REQUIRED_PASSES


def test_normalize_raises_when_redex_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(normalize, "is_available", lambda: False)
    fake_apk = tmp_path / "x.apk"
    fake_apk.write_bytes(b"")
    with pytest.raises(normalize.RedexUnavailable):
        normalize.normalize(fake_apk)


def test_assert_passes_applied_detects_missing_pass():
    with pytest.raises(normalize.RedexFailed):
        normalize._assert_passes_applied("Pass: LocalDcePass applied\n")  # no RegAllocPass


def test_assert_passes_applied_accepts_full_log():
    # Should not raise.
    normalize._assert_passes_applied("LocalDcePass ran\nRegAllocPass complete\n")


@pytest.mark.skipif(not normalize.is_available(), reason="redex not on PATH")
def test_normalize_emits_apk(tmp_path):
    # Smoke gate when redex is actually installed; we don't have an APK
    # fixture to feed it, so we just verify the API exists and the missing-APK
    # case fails loudly rather than silently producing nothing.
    fake = tmp_path / "not-an-apk.apk"
    fake.write_bytes(b"")
    with pytest.raises(normalize.RedexFailed):
        normalize.normalize(fake, out_dir=tmp_path)
