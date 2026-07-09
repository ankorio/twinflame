# twinflame — performance benchmarks

Wall-clock benchmarks of the two-phase pipeline (`prepare` → `diff`) across the whole corpus:
buildable OSS apps, large real-world apps supplied as binaries, and DEX dumps from memory. The
table shape follows Quarkslab's diffing-engine benchmark
([overview](https://blog.quarkslab.com/android-application-diffing-engine-overview.html)) —
classes compared, load time, diff time — extended with the `prepare`-phase breakdown.

- **Machine:** AMD Ryzen 5 5600X (6 cores / 12 threads), 31 GB RAM, Linux.
- **Build:** `main` working tree with the packed `.tfr` record format (interned string
  table + varints + zlib, layer-stamped), algo `97f9e01b`.
- **`prepare`** runs single-process (one sample at a time). **`diff`** runs with clustering on and
  pool-level parallelism across all 12 threads (`cluster=True, jobs=12`) — the real CLI default.
- All times are **milliseconds**. `1000 ms = 1 s`.

## Legend

| Column | Meaning |
|---|---|
| **Classes** | Classes parsed from the sample (all DEX unioned, before any diff-time filtering). |
| **Load** | Parse the APK/DEX into the in-memory model — androguard DEX parse + twinflame's direct opcode/call/string extraction. Scales ~linearly with class count. |
| **Fingerprint** | Compute the per-class SimHash signatures + abstract-opcode sequences that make later diffs cheap. Roughly equal in cost to Load. |
| **Save** | Serialize the fingerprinted record to disk as `<digest>.tfr` — packed binary (interned string table, varints, zlib; bytecode dropped, abstract opcodes kept). |
| **Prepare total** | Load + Fingerprint + Save — the full cost of turning one sample into a reusable record. |
| **Record** | On-disk size of that `.tfr` record. |
| **Matched** | Class pairs the diff reported at similarity ≥ 0.80. |
| **Record load** | Deserialize both records back from disk (the per-comparison cost that replaces re-parsing). |
| **Diff** | Time to diff two prepared records (clustering + 12-thread pools). Excludes record load, reported separately. |

**Why two phases.** `prepare` is paid once per sample and cached; a later comparison loads two
records (fast) and runs only `diff`. So preparing an app costs about the same as one load, but every
subsequent comparison against it skips parsing entirely.

---

## Prepare — representative samples (by size)

| Sample | Classes | Load | Fingerprint | Save | Prepare total | Record |
|---|--:|--:|--:|--:|--:|--:|
| Clock 2.30 (optimize) | 2,599 | 1,860 | 2,058 | 193 | 4,111 | 0.8 MB |
| Private app v1.8.2 | 8,045 | 4,761 | 3,855 | 481 | 9,097 | 1.7 MB |
| Private app v1.9.1 | 8,535 | 5,343 | 4,967 | 648 | 10,957 | 2.2 MB |
| Contacts 1.6.0 (optimize) | 11,061 | 4,823 | 4,546 | 579 | 9,948 | 2.0 MB |
| Memory DEX dump #1 | 27,159 | 13,674 | 15,682 | 1,875 | 31,232 | 6.4 MB |
| Memory DEX dump #2 | 28,169 | 14,962 | 14,331 | 1,919 | 31,212 | 6.7 MB |
| Telegram 12.7.2 | 39,108 | 19,197 | 21,757 | 2,789 | 43,743 | 8.9 MB |
| Telegram 12.8.3 | 40,090 | 20,324 | 19,897 | 2,862 | 43,083 | 9.1 MB |
| Microsoft Authenticator 6.2603.1485 | 51,131 | 31,517 | 26,610 | 5,692 | 63,818 | 10.6 MB |
| Microsoft Authenticator 6.2606.4246 | 53,345 | 34,400 | 23,999 | 5,935 | 64,335 | 11.0 MB |
| Twitter 12.0.0 | 210,120 | 103,150 | 76,190 | 9,270 | 188,610 | 26.1 MB |
| Twitter 12.5.0 | 215,381 | 114,985 | 77,741 | 9,282 | 202,008 | 26.5 MB |

**Reading it:** prepare throughput is ~**900–1,100 classes/s and essentially flat** from 2.6 k to
215 k classes — the pipeline is linear in class count, no cliff at scale. Load and Fingerprint each
take roughly half the time; Save is ~2–9 %. Records are ~**0.1–0.25 KB/class** on disk (the packed
format is ~20–25× smaller than the JSON-era records: the whole 29-sample corpus is **140 MB**,
down from 2.9 GB, with identical diff output).

## Prepare — OSS calibration matrix (built here, R8 `mapping.txt` oracle)

Three apps × two versions × three R8 profiles (`lax` = rename+shrink only, `optimize` = shipped
config, `strict` = `-allowaccessmodification -repackageclasses ''`). `optimize`/`strict` inline and
merge classes away, so they carry far fewer than `lax`.

| Sample | Classes | Load | Fingerprint | Save | Prepare total | Record |
|---|--:|--:|--:|--:|--:|--:|
| clock 2.30 / lax | 6,047 | 2,865 | 2,858 | 339 | 6,063 | 1.4 MB |
| clock 2.30 / optimize | 2,599 | 1,860 | 2,058 | 193 | 4,111 | 0.8 MB |
| clock 2.30 / strict | 2,599 | 1,881 | 2,061 | 193 | 4,135 | 0.8 MB |
| clock 2.31 / lax | 6,079 | 2,872 | 2,894 | 342 | 6,108 | 1.4 MB |
| clock 2.31 / optimize | 2,615 | 1,884 | 2,074 | 195 | 4,153 | 0.9 MB |
| clock 2.31 / strict | 2,615 | 1,906 | 2,072 | 193 | 4,171 | 0.9 MB |
| contacts 1.5.0 / lax | 11,035 | 4,815 | 4,553 | 578 | 9,946 | 2.0 MB |
| contacts 1.5.0 / optimize | 11,035 | 4,801 | 4,535 | 580 | 9,916 | 2.0 MB |
| contacts 1.5.0 / strict | 11,021 | 4,863 | 4,529 | 545 | 9,938 | 2.1 MB |
| contacts 1.6.0 / lax | 11,061 | 4,810 | 4,536 | 583 | 9,930 | 2.0 MB |
| contacts 1.6.0 / optimize | 11,061 | 4,823 | 4,546 | 579 | 9,948 | 2.0 MB |
| contacts 1.6.0 / strict | 11,044 | 4,881 | 4,541 | 544 | 9,966 | 2.1 MB |
| calc v1.4.3 / lax | 4,472 | 1,550 | 1,574 | 189 | 3,313 | 0.7 MB |
| calc v1.4.3 / optimize | 2,199 | 866 | 1,020 | 88 | 1,974 | 0.4 MB |
| calc v1.4.3 / strict | 2,093 | 869 | 1,007 | 89 | 1,965 | 0.4 MB |
| calc v1.5.2 / lax | 10,244 | 4,414 | 4,035 | 509 | 8,958 | 1.8 MB |
| calc v1.5.2 / optimize | 5,327 | 2,591 | 2,743 | 264 | 5,598 | 1.1 MB |
| calc v1.5.2 / strict | 5,136 | 2,586 | 2,736 | 266 | 5,589 | 1.2 MB |

---

## Diff — version pairs, off prepared records (clustering + 12 threads)

| Comparison | Classes (L × R) | Matched | Record load | Diff |
|---|--:|--:|--:|--:|
| Private app v1.8.2 → v1.9.1 | 8,045 × 8,535 | 11,859 | 1,313 | 4,401 |
| Telegram 12.7.2 → 12.8.3 | 39,108 × 40,090 | 21,723 | 6,014 | 35,426 |
| Microsoft Authenticator 6.2603.1485 → 6.2606.4246 | 51,131 × 53,345 | 42,719 | 9,482 | 46,930 |
| Twitter 12.0.0 → 12.5.0 | 210,120 × 215,381 | 153,375 | 23,250 | 116,439 |
| clock 2.30 → 2.31 @ lax | 6,047 × 6,079 | 3,846 | 820 | 3,733 |
| clock 2.30 → 2.31 @ optimize | 2,599 × 2,615 | 2,023 | 408 | 2,250 |
| clock 2.30 → 2.31 @ strict | 2,599 × 2,615 | 2,023 | 403 | 2,285 |
| contacts 1.5.0 → 1.6.0 @ lax | 11,035 × 11,061 | 7,961 | 1,460 | 3,144 |
| contacts 1.5.0 → 1.6.0 @ optimize | 11,035 × 11,061 | 7,961 | 1,416 | 3,203 |
| contacts 1.5.0 → 1.6.0 @ strict | 11,021 × 11,044 | 8,144 | 1,413 | 7,849 |
| calc v1.4.3 → v1.5.2 @ lax | 4,472 × 10,244 | 10,932 | 815 | 2,403 |
| calc v1.4.3 → v1.5.2 @ optimize | 2,199 × 5,327 | 5,792 | 409 | 1,832 |
| calc v1.4.3 → v1.5.2 @ strict | 2,093 × 5,136 | 4,641 | 416 | 3,203 |

**Reading it:** the whole point of clustering is here — Twitter's **210 k × 215 k** class diff finishes
in **116 s** because package clustering partitions the work into pools that run across all 12 threads.
(For reference, the same diff with clustering off — one global pool, single-threaded — had not finished
after 23 minutes; that config is only used by the accuracy oracle, where the two sides deliberately
share no package keys.) Loading both Twitter records back from disk adds **23 s** (was ~40 s for the
JSON-era records) — vs ~3.5 min to re-parse the two APKs, the reason the prepare/compare split pays.
Match counts are identical to the previous (JSON-record) run on every pair except Twitter (±1 of
153 k — the known LSH equal-Hamming tie-order sensitivity).

## Reproduce

```
# prepare every sample → corpus/records/*.tfr, plus results.jsonl (per-sample timings)
python corpus/scripts/bench_prepare.py

# diff every version pair off those records (clustering + all cores) → diff_results.jsonl
python corpus/scripts/bench_diff.py
```

Corpus layout and the `mapping.txt`-oracle accuracy harness are documented in the wiki
([Evaluation Corpus](https://github.com/ankorio/twinflame/wiki/Evaluation-Corpus),
[Accuracy Benchmarks](https://github.com/ankorio/twinflame/wiki/Accuracy-Benchmarks)).
Sample naming here keeps private/binary-only apps generic
(the private banking app and the memory DEX dumps are not named); public apps are named as-is.
