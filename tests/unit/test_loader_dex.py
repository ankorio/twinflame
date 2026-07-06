"""Tests for raw-DEX input (dumped content, no APK)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from twinflame.loader import _collect_dex_files


def test_collect_expands_directory_recursively(tmp_path: Path):
    (tmp_path / "classes.dex").write_bytes(b"x")
    (tmp_path / "classes2.dex").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "classes3.dex").write_bytes(b"x")
    (tmp_path / "not_a_dex.txt").write_bytes(b"x")  # ignored
    got = {p.name for p in _collect_dex_files(tmp_path)}
    assert got == {"classes.dex", "classes2.dex", "classes3.dex"}


def test_collect_takes_explicit_files_any_extension(tmp_path: Path):
    # A dump may name the file oddly; an explicit path is taken as-is.
    odd = tmp_path / "dumped_0x8f000000.bin"
    odd.write_bytes(b"x")
    assert _collect_dex_files([odd]) == [odd]


def test_collect_accepts_str_or_single_path(tmp_path: Path):
    f = tmp_path / "a.dex"
    f.write_bytes(b"x")
    assert _collect_dex_files(str(f)) == [f]
    assert _collect_dex_files(f) == [f]


def test_collect_dedups_overlapping_file_and_dir(tmp_path: Path):
    f = tmp_path / "classes.dex"
    f.write_bytes(b"x")
    # dir (expands to f) + the same file explicitly => f once
    out = _collect_dex_files([tmp_path, f])
    assert [p.resolve() for p in out].count(f.resolve()) == 1


def test_collect_empty_when_nothing_matches(tmp_path: Path):
    assert _collect_dex_files(tmp_path) == []


# --- clean failures on bad input (no traceback leaks) ------------------------

def test_load_raises_loaderror_on_missing_file(tmp_path: Path):
    from twinflame.loader import LoadError, load
    with pytest.raises(LoadError, match="file not found"):
        load(tmp_path / "nope.apk")


def test_load_raises_loaderror_on_non_apk(tmp_path: Path):
    from twinflame.loader import LoadError, load
    f = tmp_path / "text.apk"
    f.write_text("not an apk")
    with pytest.raises(LoadError):
        load(f)


def test_load_dex_raises_loaderror_on_empty_dir(tmp_path: Path):
    from twinflame.loader import LoadError, load_dex
    with pytest.raises(LoadError, match="no .dex files"):
        load_dex(tmp_path)


def test_load_dex_raises_loaderror_on_missing_file(tmp_path: Path):
    from twinflame.loader import LoadError, load_dex
    with pytest.raises(LoadError, match="file not found"):
        load_dex([tmp_path / "gone.dex"])


def test_load_dex_raises_loaderror_on_corrupt_dex(tmp_path: Path):
    from twinflame.loader import LoadError, load_dex
    bad = tmp_path / "junk.dex"
    bad.write_bytes(b"dex\n035\x00garbage")
    with pytest.raises(LoadError, match="could not parse DEX"):
        load_dex([bad])


# --- round-trip: raw DEX loads to the same classes as its APK ----------------
# Needs a real APK; point TWINFLAME_TEST_APK at one to enable (kept binary-free in CI).

_APK = os.environ.get("TWINFLAME_TEST_APK")


@pytest.mark.skipif(not _APK or not Path(_APK).exists(),
                    reason="set TWINFLAME_TEST_APK to a real .apk to run the DEX round-trip")
def test_load_dex_matches_apk_class_set(tmp_path: Path):
    import zipfile

    from twinflame import api

    apk_app = api.load(_APK)
    dump = tmp_path / "dump"
    dump.mkdir()
    with zipfile.ZipFile(_APK) as z:
        for name in z.namelist():
            if name.endswith(".dex") and "/" not in name:
                (dump / name).write_bytes(z.read(name))
    dex_app = api.load_dex(dump)
    assert dex_app.manifest is None  # no APK => no manifest
    assert {c.descriptor for c in dex_app.classes} == {c.descriptor for c in apk_app.classes}
