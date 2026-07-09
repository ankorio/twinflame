"""Benchmark the twinflame prepare + diff pipeline across the whole corpus.

Phase A (prepare, per sample): time load / fingerprint / save separately,
record class count + on-disk record size, persist the record to records/.
Phase B (diff, per version pair): load both prepared records and time api.diff
off them (the fast compare path — signatures precomputed, no re-parse).

Results stream to results.jsonl so a slow giant (Twitter ~210k classes) can't
lose the rows already done. Run: python bench_prepare.py
"""
from __future__ import annotations
import gc, json, sys, time
from pathlib import Path

sys.path.insert(0, "/mnt/vault/Reversing/apkdiff/twinflame")
from twinflame import api, loader
from twinflame.prepare import prepare_app, save, load_record, digest_of, _input_kind

CORPUS = Path("/mnt/vault/Reversing/apkdiff/corpus")
REC = CORPUS / "records"
REC.mkdir(exist_ok=True)
OUT = CORPUS / "scripts" / "results.jsonl"
_fh = OUT.open("w")

def emit(rec: dict):
    _fh.write(json.dumps(rec) + "\n"); _fh.flush()
    print(json.dumps(rec))

# ---- sample inventory ------------------------------------------------------
def supplied():
    base = CORPUS / "supplied_apps"
    for app in sorted(p.name for p in base.iterdir() if p.is_dir()):
        for ver in sorted(p.name for p in (base/app).iterdir() if p.is_dir()):
            yield f"{app}/{ver}", base/app/ver/"app.apk", "supplied"

def matrix():
    base = CORPUS / "oss_matrix"
    for proj in sorted(p.name for p in base.iterdir() if p.is_dir()):
        for ver in sorted(p.name for p in (base/proj).iterdir() if p.is_dir()):
            for prof in ("lax", "optimize", "strict"):
                f = base/proj/ver/prof/"app.apk"
                if f.exists():
                    yield f"{proj}/{ver}/{prof}", f, "oss_matrix"

def dumps():
    base = CORPUS / "dex_dump_sample"
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        yield f"dump/{d.name[:12]}", d, "dex_dump"

SAMPLES = list(supplied()) + list(matrix()) + list(dumps())

# name -> record path, for the diff phase
rec_path: dict[str, Path] = {}

def prepare_one(name, path, group):
    key = name.replace("/", "__")
    kind = _input_kind(Path(path))
    t0 = time.perf_counter()
    app = (loader.load_dex([Path(path)]) if kind in ("dex", "dex-dir")
           else loader.load(str(path)))
    t_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    rec = prepare_app(app, digest=digest_of(Path(path)), input_kind=kind)
    t_fp = time.perf_counter() - t1
    out = REC / f"{key}.tfr"
    t2 = time.perf_counter()
    save(rec, out)
    t_save = time.perf_counter() - t2
    rec_path[name] = out
    row = dict(phase="prepare", group=group, name=name,
               classes=len(rec.classes),
               load_ms=round(t_load*1000), fp_ms=round(t_fp*1000),
               save_ms=round(t_save*1000),
               total_ms=round((t_load+t_fp+t_save)*1000),
               record_mb=round(out.stat().st_size/2**20, 1))
    emit(row)
    del app, rec; gc.collect()

PAIRS = [
    ("cartera", "cartera/1.8.2", "cartera/1.9.1"),
    ("telegram", "telegram/12.7.2", "telegram/12.8.3"),
    ("ms_authenticator", "ms_authenticator/6.2603.1485", "ms_authenticator/6.2606.4246"),
    ("twitter", "twitter/12.0.0", "twitter/12.5.0"),
    ("clock @lax", "clock/2.30/lax", "clock/2.31/lax"),
    ("clock @optimize", "clock/2.30/optimize", "clock/2.31/optimize"),
    ("clock @strict", "clock/2.30/strict", "clock/2.31/strict"),
    ("contacts @lax", "contacts/1.5.0/lax", "contacts/1.6.0/lax"),
    ("contacts @optimize", "contacts/1.5.0/optimize", "contacts/1.6.0/optimize"),
    ("contacts @strict", "contacts/1.5.0/strict", "contacts/1.6.0/strict"),
    ("calc @lax", "calc/v1.4.3/lax", "calc/v1.5.2/lax"),
    ("calc @optimize", "calc/v1.4.3/optimize", "calc/v1.5.2/optimize"),
    ("calc @strict", "calc/v1.4.3/strict", "calc/v1.5.2/strict"),
]

def diff_pair(label, a, b):
    ra, rb = rec_path.get(a), rec_path.get(b)
    if not ra or not rb:
        return
    la = load_record(ra).app.classes
    lb = load_record(rb).app.classes
    t0 = time.perf_counter()
    matches = api.diff(list(la), list(lb), 0.8, {"cluster": False, "assignment": "auto"})
    t_diff = time.perf_counter() - t0
    emit(dict(phase="diff", label=label,
              lhs_classes=len(la), rhs_classes=len(lb),
              matches=len(matches), diff_ms=round(t_diff*1000)))
    gc.collect()

if __name__ == "__main__":
    print(f"== PREPARE {len(SAMPLES)} samples ==")
    for name, path, group in SAMPLES:
        try:
            prepare_one(name, path, group)
        except Exception as e:
            emit(dict(phase="prepare", group=group, name=name, error=repr(e)))
    print("== DIFF pairs ==")
    for label, a, b in PAIRS:
        try:
            diff_pair(label, a, b)
        except Exception as e:
            emit(dict(phase="diff", label=label, error=repr(e)))
    print("DONE")
