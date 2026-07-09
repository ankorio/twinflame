"""Diff-phase benchmark off the already-prepared records (records/*.tfr).

Runs each version pair under the *realistic* CLI config — clustering on,
pool-level parallelism across all cores (cluster=True, jobs=nproc) — which is
how the tool is actually used. Each diff runs in a worker process with a hard
timeout so one giant pair can't stall the batch. Streams to diff_results.jsonl.
"""
from __future__ import annotations
import gc, json, os, sys, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FTimeout

sys.path.insert(0, "/mnt/vault/Reversing/apkdiff/twinflame")
CORPUS = Path("/mnt/vault/Reversing/apkdiff/corpus")
REC = CORPUS / "records"
OUT = CORPUS / "scripts" / "diff_results.jsonl"
JOBS = os.cpu_count() or 1
TIMEOUT_S = 900  # 15 min ceiling per pair

PAIRS = [
    ("cartera", "cartera__1.8.2", "cartera__1.9.1"),
    ("telegram", "telegram__12.7.2", "telegram__12.8.3"),
    ("ms_authenticator", "ms_authenticator__6.2603.1485", "ms_authenticator__6.2606.4246"),
    ("twitter", "twitter__12.0.0", "twitter__12.5.0"),
    ("clock @lax", "clock__2.30__lax", "clock__2.31__lax"),
    ("clock @optimize", "clock__2.30__optimize", "clock__2.31__optimize"),
    ("clock @strict", "clock__2.30__strict", "clock__2.31__strict"),
    ("contacts @lax", "contacts__1.5.0__lax", "contacts__1.6.0__lax"),
    ("contacts @optimize", "contacts__1.5.0__optimize", "contacts__1.6.0__optimize"),
    ("contacts @strict", "contacts__1.5.0__strict", "contacts__1.6.0__strict"),
    ("calc @lax", "calc__v1.4.3__lax", "calc__v1.5.2__lax"),
    ("calc @optimize", "calc__v1.4.3__optimize", "calc__v1.5.2__optimize"),
    ("calc @strict", "calc__v1.4.3__strict", "calc__v1.5.2__strict"),
]

def _run(key_a: str, key_b: str, jobs: int) -> dict:
    # Executed in a worker process. Timing covers load_record + diff so the
    # record-deserialization cost of the giants is visible, but diff is broken
    # out separately.
    sys.path.insert(0, "/mnt/vault/Reversing/apkdiff/twinflame")
    from twinflame import api
    from twinflame.prepare import load_record
    t0 = time.perf_counter()
    la = load_record(REC / f"{key_a}.tfr").app.classes
    lb = load_record(REC / f"{key_b}.tfr").app.classes
    t_rec = time.perf_counter() - t0
    t1 = time.perf_counter()
    matches = api.diff(list(la), list(lb), 0.8,
                       {"cluster": True, "assignment": "auto", "jobs": jobs})
    return dict(lhs_classes=len(la), rhs_classes=len(lb), matches=len(matches),
                record_load_ms=round(t_rec*1000), diff_ms=round((time.perf_counter()-t1)*1000))

def main():
    fh = OUT.open("w")
    def emit(r):
        fh.write(json.dumps(r) + "\n"); fh.flush(); print(json.dumps(r))
    for label, a, b in PAIRS:
        # Inner pool does the diff's own parallelism; we give the batch a
        # 1-worker outer executor purely to enforce the wall-clock timeout.
        with ProcessPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run, a, b, JOBS)
            try:
                r = fut.result(timeout=TIMEOUT_S)
                emit(dict(phase="diff", label=label, **r))
            except FTimeout:
                emit(dict(phase="diff", label=label, timeout_s=TIMEOUT_S, note="exceeded ceiling"))
                for p in ex._processes.values():
                    p.kill()
            except Exception as e:
                emit(dict(phase="diff", label=label, error=repr(e)))
        gc.collect()
    print("DONE")

if __name__ == "__main__":
    main()
