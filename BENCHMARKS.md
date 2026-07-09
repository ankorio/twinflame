# twinflame — performance benchmarks

Wall-clock benchmarks of the two-phase pipeline (`prepare` → `diff`) across the whole corpus:
buildable OSS apps, large real-world apps supplied as binaries, and DEX dumps from memory. The
table shape follows Quarkslab's diffing-engine benchmark
([overview](https://blog.quarkslab.com/android-application-diffing-engine-overview.html)) —
classes compared, load time, diff time — extended with the `prepare`-phase breakdown.

- **Machine:** AMD Ryzen 5 5600X (6 cores / 12 threads), 31 GB RAM, Linux.
- **Build:** branch `feature/improve_load_time` @ `477df15` (direct-extraction loader), algo `4d5fcf3b`.
- **`prepare`** runs single-process (one sample at a time). **`diff`** runs with clustering on and
  pool-level parallelism across all 12 threads (`cluster=True, jobs=12`) — the real CLI default.
- All times are **milliseconds**. `1000 ms = 1 s`.

## Legend

| Column | Meaning |
|---|---|
| **Classes** | Classes parsed from the sample (all DEX unioned, before any diff-time filtering). |
| **Load** | Parse the APK/DEX into the in-memory model — androguard DEX parse + twinflame's direct opcode/call/string extraction. Scales ~linearly with class count. |
| **Fingerprint** | Compute the per-class SimHash signatures + abstract-opcode sequences that make later diffs cheap. Roughly equal in cost to Load. |
| **Save** | Serialize the fingerprinted record to disk as `<digest>.tfr.json` (bytecode dropped, abstract opcodes kept). |
| **Prepare total** | Load + Fingerprint + Save — the full cost of turning one sample into a reusable record. |
| **Record** | On-disk size of that `.tfr.json` record. |
| **Matched** | Class pairs the diff reported at similarity ≥ 0.80. |
| **Diff** | Time to diff two prepared records (clustering + 12-thread pools). Excludes record load, reported separately. |

**Why two phases.** `prepare` is paid once per sample and cached; a later comparison loads two
records (fast) and runs only `diff`. So preparing an app costs about the same as one load, but every
subsequent comparison against it skips parsing entirely.

---

## Prepare — representative samples (by size)

| Sample | Classes | Load | Fingerprint | Save | Prepare total | Record |
|---|--:|--:|--:|--:|--:|--:|
| Clock 2.30 (optimize) | 2,599 | 1,821 | 2,060 | 120 | 4,000 | 11.3 MB |
| Private app v1.8.2 | 8,045 | 4,665 | 3,864 | 999 | 9,528 | 31.7 MB |
| Private app v1.9.1 | 8,535 | 5,220 | 4,991 | 1,167 | 11,377 | 43.5 MB |
| Contacts 1.6.0 (optimize) | 11,061 | 4,810 | 4,542 | 1,301 | 10,652 | 32.2 MB |
| Memory DEX dump #1 | 27,159 | 13,690 | 15,681 | 2,005 | 31,375 | 110.6 MB |
| Memory DEX dump #2 | 28,169 | 14,999 | 14,293 | 1,071 | 30,364 | 115.3 MB |
| Telegram 12.7.2 | 39,108 | 19,823 | 19,513 | 1,566 | 40,902 | 169.2 MB |
| Telegram 12.8.3 | 40,090 | 19,728 | 19,830 | 1,576 | 41,134 | 172.5 MB |
| Microsoft Authenticator 6.2603.1485 | 51,131 | 30,580 | 26,552 | 4,415 | 61,547 | 247.9 MB |
| Microsoft Authenticator 6.2606.4246 | 53,345 | 33,327 | 23,846 | 2,402 | 59,574 | 255.2 MB |
| Twitter 12.0.0 | 210,120 | 100,170 | 76,695 | 16,787 | 193,652 | 660.0 MB |
| Twitter 12.5.0 | 215,381 | 112,887 | 77,387 | 19,184 | 209,458 | 672.3 MB |

**Reading it:** prepare throughput is ~**900–1,100 classes/s and essentially flat** from 2.6 k to
215 k classes — the pipeline is linear in class count, no cliff at scale. Load and Fingerprint each
take roughly half the time; Save is ~10 %. Records are ~3–4 KB/class on disk.

## Prepare — OSS calibration matrix (built here, R8 `mapping.txt` oracle)

Three apps × two versions × three R8 profiles (`lax` = rename+shrink only, `optimize` = shipped
config, `strict` = `-allowaccessmodification -repackageclasses ''`). `optimize`/`strict` inline and
merge classes away, so they carry far fewer than `lax`.

| Sample | Classes | Load | Fingerprint | Save | Prepare total | Record |
|---|--:|--:|--:|--:|--:|--:|
| clock 2.30 / lax | 6,047 | 2,806 | 2,844 | 717 | 6,367 | 19.3 MB |
| clock 2.30 / optimize | 2,599 | 1,821 | 2,060 | 120 | 4,000 | 11.3 MB |
| clock 2.30 / strict | 2,599 | 1,837 | 2,062 | 108 | 4,007 | 11.3 MB |
| clock 2.31 / lax | 6,079 | 2,793 | 2,865 | 703 | 6,361 | 19.4 MB |
| clock 2.31 / optimize | 2,615 | 1,838 | 2,060 | 109 | 4,007 | 11.4 MB |
| clock 2.31 / strict | 2,615 | 1,862 | 2,062 | 109 | 4,033 | 11.4 MB |
| contacts 1.5.0 / lax | 11,035 | 4,785 | 5,219 | 620 | 10,625 | 32.1 MB |
| contacts 1.5.0 / optimize | 11,035 | 4,856 | 5,187 | 640 | 10,684 | 32.1 MB |
| contacts 1.5.0 / strict | 11,021 | 4,847 | 4,520 | 1,274 | 10,642 | 31.1 MB |
| contacts 1.6.0 / lax | 11,061 | 4,824 | 4,535 | 1,271 | 10,630 | 32.2 MB |
| contacts 1.6.0 / optimize | 11,061 | 4,810 | 4,542 | 1,301 | 10,652 | 32.2 MB |
| contacts 1.6.0 / strict | 11,044 | 4,838 | 4,522 | 1,248 | 10,608 | 31.1 MB |
| calc v1.4.3 / lax | 4,472 | 1,525 | 1,571 | 440 | 3,536 | 10.2 MB |
| calc v1.4.3 / optimize | 2,199 | 848 | 1,023 | 208 | 2,079 | 5.4 MB |
| calc v1.4.3 / strict | 2,093 | 839 | 1,017 | 204 | 2,060 | 5.2 MB |
| calc v1.5.2 / lax | 10,244 | 4,409 | 4,016 | 965 | 9,391 | 27.6 MB |
| calc v1.5.2 / optimize | 5,327 | 2,497 | 2,747 | 182 | 5,426 | 15.9 MB |
| calc v1.5.2 / strict | 5,136 | 2,443 | 2,711 | 163 | 5,316 | 15.4 MB |

---

## Diff — version pairs, off prepared records (clustering + 12 threads)

| Comparison | Classes (L × R) | Matched | Diff |
|---|--:|--:|--:|
| Private app v1.8.2 → v1.9.1 | 8,045 × 8,535 | 11,859 | 4,154 |
| Telegram 12.7.2 → 12.8.3 | 39,108 × 40,090 | 21,723 | 36,452 |
| Microsoft Authenticator 6.2603.1485 → 6.2606.4246 | 51,131 × 53,345 | 42,719 | 50,921 |
| Twitter 12.0.0 → 12.5.0 | 210,120 × 215,381 | 153,374 | 126,062 |
| clock 2.30 → 2.31 @ lax | 6,047 × 6,079 | 3,846 | 3,353 |
| clock 2.30 → 2.31 @ optimize | 2,599 × 2,615 | 2,023 | 1,931 |
| clock 2.30 → 2.31 @ strict | 2,599 × 2,615 | 2,023 | 1,944 |
| contacts 1.5.0 → 1.6.0 @ lax | 11,035 × 11,061 | 7,961 | 2,992 |
| contacts 1.5.0 → 1.6.0 @ optimize | 11,035 × 11,061 | 7,961 | 3,230 |
| contacts 1.5.0 → 1.6.0 @ strict | 11,021 × 11,044 | 8,144 | 8,089 |
| calc v1.4.3 → v1.5.2 @ lax | 4,472 × 10,244 | 10,932 | 2,006 |
| calc v1.4.3 → v1.5.2 @ optimize | 2,199 × 5,327 | 5,792 | 1,587 |
| calc v1.4.3 → v1.5.2 @ strict | 2,093 × 5,136 | 4,641 | 3,103 |

**Reading it:** the whole point of clustering is here — Twitter's **210 k × 215 k** class diff finishes
in **126 s** because package clustering partitions the work into pools that run across all 12 threads.
(For reference, the same diff with clustering off — one global pool, single-threaded — had not finished
after 23 minutes; that config is only used by the accuracy oracle, where the two sides deliberately
share no package keys.) Loading the two Twitter records back from disk adds ~40 s on top.

## Reproduce

```
# prepare every sample → corpus/records/*.tfr.json, plus results.jsonl (per-sample timings)
python corpus/scripts/bench_prepare.py

# diff every version pair off those records (clustering + all cores) → diff_results.jsonl
python corpus/scripts/bench_diff.py
```

Corpus layout and the `mapping.txt`-oracle accuracy harness are documented in the wiki
([Evaluation Corpus](https://github.com/ankorio/twinflame/wiki/Evaluation-Corpus),
[Accuracy Benchmarks](https://github.com/ankorio/twinflame/wiki/Accuracy-Benchmarks)).
Sample naming here keeps private/binary-only apps generic
(the private banking app and the memory DEX dumps are not named); public apps are named as-is.
