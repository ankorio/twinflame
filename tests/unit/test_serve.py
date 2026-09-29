"""`twinflame serve`: the in-process dispatcher and the NDJSON loop, end to end
over synthetic `.tfr` records (no APK emitter in the tree)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402

from twinflame.model import App  # noqa: E402
from twinflame.prepare import SIGNATURE_STAMP, prepare_app, save  # noqa: E402
from twinflame import serve  # noqa: E402

pytest.importorskip(
    "twinflame_libsigs", reason="twinflame_libsigs not installed (companion project)")

_RET = ["V", "I", "J", "F", "Ljava/lang/String;", "Z", "[I", "D"]


def _cls(descriptor, seed, *, strings=()):
    methods = tuple(
        syn.MethodSpec(name=f"m{j}", arg_count=(seed * 7 + j * 3) % 4,
                       return_type=_RET[(seed * 7 + j * 3) % len(_RET)],
                       instr_count=25 + j, xref_count=(seed + j) % 5,
                       calls=(f"Landroid/os/C{(seed * 7 + j) % 6};->go()V",
                              f"Ljava/util/L{seed % 5};->q()I"))
        for j in range(4))
    return syn.make_class(syn.ClassSpec(descriptor=descriptor, methods=methods,
                                        strings=tuple(strings)))


def _write_record(path: Path, classes, digest):
    app = App(path=path, classes=tuple(classes), manifest=None)
    save(prepare_app(app, digest=digest, input_kind="dex"), path)


@pytest.fixture
def inputs(tmp_path):
    """Two 'inputs' sharing `La/Shared;` (same structure in both), each with
    its own class — the APK + dump situation."""
    a = tmp_path / "app.tfr"
    b = tmp_path / "dump.tfr"
    _write_record(a, [_cls("La/Shared;", 1), _cls("La/OnlyApk;", 3)], "dig-a")
    _write_record(b, [_cls("La/Shared;", 1, strings=("http://c2.example/x",)),
                      _cls("La/Payload;", 4)], "dig-b")
    return a, b


def _session(tmp_path):
    return serve.Session(cache_dir=tmp_path / "cache", use_libsigs=False, log=lambda m: None)


def _call(session, op, **params):
    resp = serve.dispatch(session, {"v": 1, "id": op, "op": op, "params": params})
    assert resp["ok"], resp
    return resp["result"]


def _load(session, inputs):
    a, b = inputs
    return _call(session, "load", inputs=[{"index": 0, "path": str(a)}, {"index": 1, "path": str(b)}])


# ---- protocol -----------------------------------------------------------------

def test_hello_reports_stamp_and_protocol(tmp_path):
    r = _call(_session(tmp_path), "hello")
    assert r["protocol"] == serve.PROTOCOL_VERSION
    assert r["stamp"] == SIGNATURE_STAMP
    assert "twinflame" in r and "python" in r


def test_unknown_op_and_bad_params_are_errors_not_crashes(tmp_path):
    s = _session(tmp_path)
    r = serve.dispatch(s, {"v": 1, "id": 7, "op": "nope"})
    assert r == {"id": 7, "ok": False, "error": {"code": "unknown-op", "message": r["error"]["message"]}}
    r = serve.dispatch(s, {"v": 1, "id": 8, "op": "load", "params": {}})
    assert not r["ok"] and r["error"]["code"] == "bad-params"
    r = serve.dispatch(s, {"v": 99, "id": 9, "op": "hello"})
    assert not r["ok"] and r["error"]["code"] == "bad-version"
    r = serve.dispatch(s, "not an object")
    assert not r["ok"] and r["error"]["code"] == "bad-request"


# ---- load / where / describe ----------------------------------------------------

def test_load_keeps_inputs_separate_and_reports_duplicates(tmp_path, inputs):
    s = _session(tmp_path)
    r = _load(s, inputs)
    assert [row["classes"] for row in r["inputs"]] == [2, 2]
    assert [row["kind"] for row in r["inputs"]] == ["tfr", "tfr"]
    assert r["stamp"] == SIGNATURE_STAMP

    w = _call(s, "where", descriptors=["La/Shared;", "La/Payload;", "La/Nope;"])["where"]
    assert w == {"La/Shared;": [0, 1], "La/Payload;": [1], "La/Nope;": []}

    d = _call(s, "describe", input_index=1, descriptor="La/Shared;")
    assert d["duplicates"] == [0]
    assert len(d["signature"]) == 32 and int(d["signature"], 16) > 0
    assert d["instructions"] == sum(25 + j for j in range(4))
    assert d["methods"] == 4 and d["libsigs"] is None
    assert d["below_min_instructions"] is False

    d0 = _call(s, "describe", input_index=0, descriptor="La/Shared;")
    assert d0["signature"] == d["signature"]      # same structure, same signature
    assert d0["duplicates"] == [1]
    assert d["anchor_strings"] == ["http://c2.example/x"] and d0["anchor_strings"] == []
    assert d["method_ids"] and all(m.startswith("m") and "(" in m for m in d["method_ids"])


def test_describe_unknown_class_or_input(tmp_path, inputs):
    s = _session(tmp_path)
    _load(s, inputs)
    r = serve.dispatch(s, {"v": 1, "id": 1, "op": "describe",
                           "params": {"input_index": 0, "descriptor": "La/Payload;"}})
    assert not r["ok"] and r["error"]["code"] == "no-such-class"
    r = serve.dispatch(s, {"v": 1, "id": 2, "op": "describe",
                           "params": {"input_index": 5, "descriptor": "La/Payload;"}})
    assert not r["ok"] and r["error"]["code"] == "no-such-input"


def test_load_reports_a_broken_input_in_its_row(tmp_path, inputs):
    s = _session(tmp_path)
    bad = tmp_path / "junk.bin"
    bad.write_bytes(b"\x00\x01\x02\x03 nothing")
    r = _call(s, "load", inputs=[{"index": 0, "path": str(inputs[0])}, {"index": 1, "path": str(bad)}])
    assert r["inputs"][0]["classes"] == 2
    assert r["inputs"][1]["error"]["code"] == "unknown-kind"
    assert sorted(s.inputs) == [0]


def test_detect_kind_by_magic_not_extension(tmp_path):
    dex = tmp_path / "payload.bin"
    dex.write_bytes(b"dex\n035\0" + b"\0" * 64)
    assert serve.detect_kind(dex) == "dex"
    zipf = tmp_path / "sample.apk"
    zipf.write_bytes(b"PK\x03\x04" + b"\0" * 32)
    assert serve.detect_kind(zipf) == "apk"
    assert serve.detect_kind(tmp_path) == "dex-dir"
    with pytest.raises(serve.ServeError) as e:
        serve.detect_kind(tmp_path / "missing.dex")
    assert e.value.code == "not-found"


# ---- pack.build / pack.match / pack.list ------------------------------------------

def test_pack_build_then_match_finds_payload_in_the_dump_only(tmp_path, inputs):
    s = _session(tmp_path)
    _load(s, inputs)
    out = tmp_path / "db" / "packs" / "01-tester"
    b = _call(s, "pack.build", name="evilfam",
              entries=[{"input_index": 1, "descriptor": "La/Payload;", "note": "the dropper",
                        "methods": ["m0()V", "nope()V"]},
                       {"input_index": 1, "descriptor": "La/Shared;"}],
              out_dir=str(out), meta={"family": "evilfam", "tags": ["dropper"]}, radius=6)
    assert b["n_entries"] == 2 and b["radius_build"] == 6 and b["stamp"] == SIGNATURE_STAMP
    assert Path(b["path"]).is_file() and Path(b["sidecar"]).is_file()
    assert len(b["sha256"]["pack"]) == 64
    sidecar = json.loads(Path(b["sidecar"]).read_text())
    assert sidecar["meta"]["curated"] is True and sidecar["meta"]["tags"] == ["dropper"]
    assert sidecar["payloads"]["0"]["note"] == "the dropper"
    assert sidecar["payloads"]["0"]["descriptor"] == "La/Payload;"
    assert sidecar["payloads"]["0"]["input_index"] == 1
    assert sidecar["payloads"]["1"]["note"] == ""
    assert "methods" not in sidecar["payloads"]["1"]
    meth = sidecar["payloads"]["0"]["methods"]
    assert len(meth) == 1 and meth[0].startswith("m0(")   # unknown method ids are dropped

    events = []
    resp = serve.dispatch(s, {"v": 1, "id": "m", "op": "pack.match",
                              "params": {"packs": [{"id": "evil", "path": b["path"]}]}},
                          emit=events.append)
    assert resp["ok"], resp
    rows = {r["input_index"]: r for r in resp["result"]["results"]}
    assert set(rows) == {0, 1} and resp["result"]["skipped"] == []
    # the dump has both entries, the apk only the shared one
    assert rows[1]["present"] == 2 and rows[1]["score"] == 1.0
    assert rows[0]["present"] == 1 and 0 < rows[0]["score"] < 1
    hit = next(h for h in rows[1]["hits"] if h["entry_id"] == 0)
    assert hit["descriptor"] == "La/Payload;" and hit["tier"] == 1 and hit["distance"] == 0
    assert hit["note"] == "the dropper"
    assert rows[1]["radius"] == 6            # pack's radius_build is the default
    assert [e["event"] for e in events] == ["progress", "progress"]
    assert all(e["id"] == "m" for e in events)

    lst = _call(s, "pack.list", dir=str(tmp_path / "db"))["packs"]
    assert len(lst) == 1 and lst[0]["stamp_ok"] and lst[0]["n_entries"] == 2
    assert lst[0]["meta"]["family"] == "evilfam"


def test_pack_match_extra_entries_and_stale_pack(tmp_path, inputs):
    s = _session(tmp_path)
    _load(s, inputs)
    b = _call(s, "pack.build", name="f", out_dir=str(tmp_path / "p"),
              entries=[{"input_index": 0, "descriptor": "La/OnlyApk;"}])
    payload_sig = _call(s, "describe", input_index=1, descriptor="La/Payload;")["signature"]
    r = _call(s, "pack.match", inputs=[1],
              packs=[{"id": "f", "path": b["path"],
                      "extra_entries": [{"signature": payload_sig, "fqcn": "a.Payload",
                                         "instructions": 100, "note": "confirmed"}]}])
    (row,) = r["results"]
    assert row["present"] == 1
    (hit,) = row["hits"]
    assert hit["entry_id"] == 1 and hit["extra"] is True and hit["note"] == "confirmed"

    # a pack from another signature algorithm is skipped, not fatal
    from twinflame_libsigs.pack import write_pack
    stale = tmp_path / "stale.tflp"
    write_pack(stale, [(0, 1, ())], {"0": {"fqcn": "x"}}, sig_stamp="0000deadbeef0000",
               meta={"kind": "family", "family": "old"})
    r = _call(s, "pack.match", packs=[{"path": str(stale)}, {"path": b["path"]}])
    assert [k["reason"] for k in r["skipped"]] == ["stale-stamp"]
    assert {row["pack_id"] for row in r["results"]} == {"pack"}
    lst = _call(s, "pack.list", dir=str(tmp_path), recursive=False)["packs"]
    assert [p["stamp_ok"] for p in lst if p["path"] == str(stale)] == [False]


def test_pack_build_needs_loaded_classes(tmp_path, inputs):
    s = _session(tmp_path)
    _load(s, inputs)
    r = serve.dispatch(s, {"v": 1, "id": 1, "op": "pack.build", "params": {
        "name": "x", "out_dir": str(tmp_path / "o"),
        "entries": [{"input_index": 0, "descriptor": "La/Payload;"}]}})
    assert not r["ok"] and r["error"]["code"] == "no-such-class"


# ---- cache ---------------------------------------------------------------------------

def test_cache_dir_is_used_for_non_record_inputs(tmp_path, monkeypatch):
    """A synthetic App stands in for the APK parse; the second load must come
    from the cached record without touching the loader."""
    from twinflame import loader

    calls = []

    def fake_load(path, *, redex_normalize=False):
        calls.append(path)
        return App(path=Path(path), classes=(_cls("La/X;", 2),), manifest=None)

    monkeypatch.setattr(loader, "load", fake_load)
    apk = tmp_path / "s.apk"
    apk.write_bytes(b"PK\x03\x04" + b"\0" * 16)
    s = _session(tmp_path)
    r1 = _call(s, "load", inputs=[{"index": 0, "path": str(apk)}])
    assert r1["inputs"][0]["from_cache"] is False and r1["inputs"][0]["kind"] == "apk"
    assert list((tmp_path / "cache").glob("*.tfr"))
    r2 = _call(s, "load", inputs=[{"index": 0, "path": str(apk)}])
    assert r2["inputs"][0]["from_cache"] is True
    assert r2["inputs"][0]["digest"] == r1["inputs"][0]["digest"]
    assert len(calls) == 1


# ---- the loop ------------------------------------------------------------------------

def test_ndjson_loop_end_to_end(tmp_path, inputs):
    a, b = inputs
    lines = [
        {"v": 1, "id": 1, "op": "hello"},
        "this is not json",
        {"v": 1, "id": 2, "op": "load", "params": {"inputs": [{"index": 0, "path": str(a)},
                                                                {"index": 1, "path": str(b)}]}},
        {"v": 1, "id": 3, "op": "where", "params": {"descriptors": ["La/Payload;"]}},
        {"v": 1, "id": 4, "op": "shutdown"},
        {"v": 1, "id": 5, "op": "hello"},   # after shutdown: never answered
    ]
    stdin = io.StringIO("".join((json.dumps(l) if isinstance(l, dict) else l) + "\n" for l in lines))
    stdout = io.StringIO()
    rc = serve.run(_session(tmp_path), stdin=stdin, stdout=stdout)
    assert rc == 0
    out = [json.loads(l) for l in stdout.getvalue().splitlines()]
    ids = [o.get("id") for o in out]
    assert ids == [1, None, 2, 2, 2, 3, 4], out   # two progress events for load
    assert out[1]["error"]["code"] == "bad-json"
    assert out[2]["event"] == "progress" and out[4]["ok"]
    assert out[5]["result"]["where"] == {"La/Payload;": [1]}
    assert out[6]["result"] == {"bye": True}
