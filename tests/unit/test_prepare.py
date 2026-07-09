from __future__ import annotations

import sys
from pathlib import Path

import pytest

from twinflame.model import App
from twinflame.prepare import (
    ALGO_VERSION,
    RECORD_VERSION,
    load_record,
    prepare_app,
    record_from_dict,
    record_to_dict,
    save,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402


def _record(n=4, digest="abc123"):
    classes = syn.vendor_app(n_classes=n)
    return prepare_app(App(Path(digest), tuple(classes), None),
                       digest=digest, input_kind="apk", normalized=False)


def test_algo_version_is_stable_hex():
    assert isinstance(ALGO_VERSION, str) and len(ALGO_VERSION) == 16
    assert int(ALGO_VERSION, 16) >= 0  # hex


def test_record_carries_digest_and_stamps():
    rec = _record(digest="deadbeef")
    assert rec.digest == "deadbeef"
    assert rec.algo_version == ALGO_VERSION
    assert rec.record_version == RECORD_VERSION
    assert rec.input_kind == "apk" and rec.normalized is False


def test_record_app_view_feeds_the_model():
    rec = _record()
    app = rec.app
    assert len(app.classes) == len(rec.classes)
    assert app.manifest is rec.manifest


def test_serialize_roundtrip_preserves_classes():
    rec = _record(n=6)
    back = record_from_dict(record_to_dict(rec))
    assert back.digest == rec.digest
    assert [c.descriptor for c in back.classes] == [c.descriptor for c in rec.classes]
    for a, b in zip(rec.classes, back.classes):
        assert a.signature == b.signature
        assert [m.abstract for m in a.methods] == [m.abstract for m in b.methods]
        assert [m.calls for m in a.methods] == [m.calls for m in b.methods]


def test_save_and_load_record(tmp_path):
    rec = _record(digest="cafef00d")
    p = save(rec, tmp_path / "rec.json")
    loaded = load_record(p)
    assert loaded.digest == "cafef00d"
    assert [c.descriptor for c in loaded.classes] == [c.descriptor for c in rec.classes]


def test_load_record_rejects_stale_algo_version(tmp_path):
    rec = _record()
    d = record_to_dict(rec)
    d["abstract_version"] = "0000000000000000"  # simulate an opcode-table change
    p = tmp_path / "stale.json"
    import json

    p.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="re-prepare"):
        load_record(p)
