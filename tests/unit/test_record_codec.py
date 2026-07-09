"""Packed binary .tfr codec — exact round-trip and rejection paths.

The integration suite proves a record is lossless *for comparison*; these
tests pin the stronger property the packed format is built on: decode(encode(r))
reproduces every field of the record exactly (dataclass equality, recursive
through classes/methods/fields/manifest), and stale or foreign files are
rejected with a clear error instead of garbage.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import synthetic

from twinflame.model import App, ManifestInfo, Signature
from twinflame.prepare import (
    MAGIC,
    _legacy_v1_algo_version,
    is_record_file,
    load_record,
    migrate_record,
    prepare_app,
    record_from_bytes,
    record_to_bytes,
    record_to_dict,
    save,
)


def _record(manifest=None, n_classes=4, digest="d" * 64):
    app = App(Path("x"), tuple(synthetic.vendor_app(n_classes=n_classes)), manifest)
    return prepare_app(app, digest=digest, input_kind="apk")


def test_roundtrip_exact_equality():
    rec = _record()
    assert record_from_bytes(record_to_bytes(rec)) == rec


def test_roundtrip_with_manifest():
    mf = ManifestInfo(package="com.example", activities=("com.example.Main", ""),
                      services=(), receivers=("com.example.Boot",), providers=())
    rec = _record(manifest=mf)
    back = record_from_bytes(record_to_bytes(rec))
    assert back.manifest == mf
    assert back == rec


def test_roundtrip_preserves_header_fields():
    rec = _record(digest="custom-pipeline-key")  # digest is not required to be hex
    back = record_from_bytes(record_to_bytes(rec))
    assert (back.digest, back.input_kind, back.normalized, back.algo_version) == \
           (rec.digest, rec.input_kind, rec.normalized, rec.algo_version)


def test_save_load_file_roundtrip(tmp_path):
    rec = _record()
    p = save(rec, tmp_path / "sample.tfr")
    assert is_record_file(p)
    assert load_record(p) == rec


def test_bad_magic_rejected():
    with pytest.raises(ValueError, match="bad magic"):
        record_from_bytes(b"NOPE" + b"\x00" * 16)


def test_unsupported_layout_version_rejected():
    data = bytearray(record_to_bytes(_record()))
    data[3] = 99
    with pytest.raises(ValueError, match="layout version 99"):
        record_from_bytes(bytes(data))


def _legacy_json(rec, algo_version):
    """Serialize `rec` the way v1 code did: monolithic stamp, no layer keys."""
    d = record_to_dict(rec)
    for k in ("extraction_version", "abstract_version", "signature_version"):
        del d[k]
    d["record_version"] = 1
    d["algo_version"] = algo_version
    return json.dumps(d)


def test_legacy_json_unknown_stamp_reports_stale(tmp_path):
    """A v1 record whose stamp doesn't match today's parameters can't say
    which layer changed — rejected as needing a re-prepare, and refused by
    migrate_record."""
    p = tmp_path / "old.tfr.json"
    p.write_text(_legacy_json(_record(), "0" * 16))
    assert is_record_file(p)
    with pytest.raises(ValueError, match="re-prepare"):
        load_record(p)
    assert migrate_record(load_record(p, check=False)) is None


def test_legacy_json_current_content_loads_and_migrates(tmp_path):
    """A v1 record whose stamp proves its content matches today's parameters
    is directly loadable, and migrate re-encodes it as a packed v2 record."""
    rec = _record()
    p = tmp_path / "ok.tfr.json"
    p.write_text(_legacy_json(rec, _legacy_v1_algo_version()))
    loaded = load_record(p)  # no error: every layer provably current
    assert loaded.stale_layers() == ()
    migrated = migrate_record(loaded)
    assert migrated == rec  # includes record_version normalized to v2
    assert record_from_bytes(record_to_bytes(migrated)) == rec


def test_migrate_recomputes_stale_signature_layer():
    """Signature-param drift is fixed in place: signatures rebuilt from the
    stored abstract sequences, other layers untouched."""
    rec = _record()
    garbage = tuple(replace(c, signature=Signature(1, 2, 3, 4)) for c in rec.classes)
    stale = replace(rec, signature_version="ab" * 8, classes=garbage)
    assert stale.stale_layers() == ("signature",)

    migrated = migrate_record(stale)
    assert migrated == rec  # signatures identical to a fresh prepare


def test_migrate_refuses_non_recomputable_layers():
    rec = _record()
    assert migrate_record(replace(rec, abstract_version="ab" * 8)) is None
    assert migrate_record(replace(rec, extraction_version="ab" * 8)) is None


def test_stale_record_error_names_migrate_when_fixable(tmp_path):
    rec = replace(_record(), signature_version="ab" * 8)
    p = save(rec, tmp_path / "stale.tfr")
    with pytest.raises(ValueError, match="migrate"):
        load_record(p)
    assert load_record(p, check=False).stale_layers() == ("signature",)


def test_is_record_file_negative(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK\x03\x04junk")
    assert not is_record_file(apk)
    assert not is_record_file(tmp_path)  # directory
    # JSON that isn't named like a record is not sniffed as one
    j = tmp_path / "report.json"
    j.write_text("{}")
    assert not is_record_file(j)


def test_roundtrip_unpaired_surrogate_strings():
    """DEX strings are MUTF-8: unpaired surrogates occur in real APKs (found
    by the first bulk corpus migration) and must round-trip losslessly."""
    rec = _record()
    weird = replace(rec.classes[0], strings=("ok\uda91bad", "\ud800"))
    rec = replace(rec, classes=(weird,) + rec.classes[1:])
    back = record_from_bytes(record_to_bytes(rec))
    assert back.classes[0].strings == ("ok\uda91bad", "\ud800")
    assert back == rec


def test_magic_is_stable_prefix():
    assert record_to_bytes(_record())[:4] == MAGIC
