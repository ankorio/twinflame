"""`twinflame family build|match` CLI: end-to-end over synthetic .tfr records."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import synthetic as syn  # noqa: E402

from twinflame.model import App  # noqa: E402
from twinflame.prepare import prepare_app, save  # noqa: E402

pytest.importorskip(
    "twinflame_libsigs", reason="twinflame_libsigs not installed (companion project)")

from twinflame import cli  # noqa: E402

_RET = ["V", "I", "J", "F", "Ljava/lang/String;", "Z", "[I", "D"]


def _cls(descriptor, seed):
    methods = tuple(
        syn.MethodSpec(name=f"m{j}", arg_count=(seed * 7 + j * 3) % 4,
                       return_type=_RET[(seed * 7 + j * 3) % len(_RET)],
                       instr_count=25 + j, xref_count=(seed + j) % 5,
                       calls=(f"Landroid/os/C{(seed * 7 + j) % 6};->go()V",
                              f"Ljava/util/L{seed % 5};->q()I"))
        for j in range(4))
    return syn.make_class(syn.ClassSpec(descriptor=descriptor, methods=methods))


def _write_record(path: Path, classes, digest):
    app = App(path=path, classes=tuple(classes), manifest=None)
    save(prepare_app(app, digest=digest, input_kind="dex-dir"), path)


@pytest.fixture
def family_records(tmp_path):
    # Three samples sharing two classes (seed 1, 2); each has a unique class.
    shared = [_cls("La/Shared1;", 1), _cls("La/Shared2;", 2)]
    recs = []
    for i, uniq in enumerate([3, 4, 5]):
        p = tmp_path / f"sample{i}.tfr"
        _write_record(p, shared + [_cls(f"La/Only{i};", uniq)], digest=f"dig{i}")
        recs.append(p)
    return recs


def test_family_build_and_match_json(tmp_path, family_records, capsys):
    pack = tmp_path / "fam.tflp"
    rc = cli.main(["family", "build", "-o", str(pack), "--name", "evilfam",
                   "--no-libsigs", "-k", "2", "--min-instr", "5", *map(str, family_records)])
    assert rc == 0 and pack.exists()
    build_out = capsys.readouterr().out
    assert "family pack" in build_out

    rc = cli.main(["family", "match", "--json", "--min-instr", "5",
                   str(pack), str(family_records[0])])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["family"] == "evilfam"
    assert result["present"] >= 1 and 0.0 < result["score"] <= 1.0


def test_family_match_threshold_exit_codes(tmp_path, family_records, capsys):
    pack = tmp_path / "fam.tflp"
    cli.main(["family", "build", "-o", str(pack), "--no-libsigs", "-k", "1",
              "--min-instr", "5", *map(str, family_records)])
    capsys.readouterr()

    # sample 0 contains its own classes -> some containment -> clears a low bar
    assert cli.main(["family", "match", "--min-instr", "5", "--threshold", "0.1",
                     str(pack), str(family_records[0])]) == 0
    assert cli.main(["family", "match", "--min-instr", "5", "--threshold", "0.999",
                     str(pack), str(family_records[0])]) == 1


def test_family_build_rejects_single_sample(tmp_path, family_records):
    with pytest.raises(SystemExit):
        cli.main(["family", "build", "-o", str(tmp_path / "x.tflp"),
                  "--no-libsigs", str(family_records[0])])


def test_family_unknown_subcommand_returns_2(capsys):
    assert cli.main(["family", "bogus"]) == 2
