# Using twinflame

A task-oriented guide. For *how it works internally* see [`how-it-works.md`](how-it-works.md);
for the machine output format see [`change-report-schema.md`](change-report-schema.md).

twinflame compares two Android builds at the **DEX/class level** and tells you what changed —
even when R8/ProGuard has renamed everything. It's a CLI that emits data (matches, a change
report, a deobfuscation map); you visualize with your own decompiler.

## Inputs

Each side is an APK, a single `.dex`, or a directory of `.dex` files (for
dumped/extracted DEX with no APK). The input kind is detected — no flag needed:

```sh
twinflame old.apk new.apk                 # two APKs
twinflame dump_old/ dump_new/             # two directories of dumped classes*.dex
twinflame a/classes.dex b/classes.dex     # single .dex each
```

DEX-only input has no manifest, so pass `--app-package <prefix>` (below) instead of `--auto-package`.

---

## UC1 — what changed between two versions of my app

The main use case. Get a ranked, method-localized change report. The semantic
diff is the default output: a human summary prints to stdout and the machine
report is written to a file automatically.

```sh
twinflame old.apk new.apk \
    --app-package com.myapp \        # your app's package root(s); repeat for multi-root
    -o changes.json                   # machine-readable (the real deliverable; JSON default)
```

Pick the file format with `-f {json,csv,xml}`, or `--no-file` to print only the
summary. Emit just the changed classes with `-s changed`, or narrow to one class
with `-c <name>`.

**Read the summary top-down.** The report is pre-sorted so the first things you see are the
developer's own (`app`) changes, high-confidence first:

```
change summary: modified=384  added=282  removed=135  cosmetic=414  unchanged=19649
  modified confidence: high=384  low=0 (low = call-only churn in non-app code, likely cross-toolchain noise)
  app-only:     modified=384  added=282  removed=135  cosmetic=414  unchanged=10436

top modified (app-first, high-confidence first, then by magnitude):
  [app] [ 134] SourceFile  +47 calls, -40 calls, +6 strings, +9 types, +17 methods  [anchored]
          - onCreate(Landroid/os/Bundle;)V: method +18 calls, -8 calls
```

- **`cosmetic` / `unchanged`** are re-obfuscation/rebuild noise — ignore them.
- **`app-only`** is the number that matters: the developer's own changed classes.
- If the two builds used **different toolchains** (months apart), add `--min-confidence high` to
  drop `low`-confidence library churn (call-only relocation from different R8 inlining).
- Each `modified` localizes to the **methods** that changed and how (see `methods[]` in the JSON).

Feed `changes.json` to your tooling to open exactly those classes side-by-side in a decompiler.

---

## UC2 — do these two apps share code (e.g. malware kinship)?

Two *different* apps with no shared package layout. Drop clustering so classes match across
package boundaries, and rely on rename-invariant anchors:

```sh
twinflame sample_a.apk sample_b.apk --no-cluster --find-obfuscated \
    -o pairs.json --components-file shared-components.txt
```

`pairs.json` is the ranked list of strongly-similar class pairs (the shared-code islands);
treat the high-similarity pairs as the kinship signal.

For **malware triage**, watch the `sensitive components shared by both builds` block (printed to
stdout, and dumped to `--components-file`). twinflame resolves each class's superclass/interface
chain — which R8 can't rename — and flags when both samples implement the same permission-gated
component: `BNL` (NotificationListener), `BAS` (AccessibilityService), `BDA` (DeviceAdmin),
`BIM` (InputMethod), `BAF` (Autofill). Two samples sharing an `AccessibilityService`
implementation is a strong same-family tell. Every paired row in the JSON/CSV/XML also carries
`lhs_super`/`rhs_super`, `interfaces`, and any `components` code.

---

## UC3 — recover original names (deobfuscation)

Produce a ProGuard-style `mapping.txt` that renames the new build's obfuscated classes using
names recovered from a clear-named (older/donor) build:

```sh
twinflame old.apk new.apk --deobfuscation-map recovered.txt
```

Then hand it to a decompiler / retrace:

```sh
# jadx: apply the mapping while decompiling
jadx --rename-flags none --deobf ... new.apk        # or import recovered.txt as a mapping
# R8/ProGuard retrace a stack trace:
retrace recovered.txt crash.txt
```

Only high-confidence matches are written (tune with `--map-min-confidence`; anchored matches are
always included).

---

## Precompute once, compare many (`prepare`)

The one-shot `twinflame a.apk b.apk` re-parses both APKs every run — and the parse
(androguard) is the expensive part (tens of seconds per app). For pipelines that
compare a sample against many others, split the work: fingerprint each sample **once**
into a reusable, digest-keyed **record**, then compare from records (no re-parse).

```sh
# fingerprint a sample into a record (parse + signatures + abstract opcodes)
twinflame prepare sample.apk --digest <sha256> -o sample.tfr.json
```

A record is **lossless for comparison** — a diff off a record is identical to a diff
off a fresh parse — and it's version-stamped (`algo_version`), so a stale record is
rejected rather than silently mis-compared. Loading a record back is ~40× faster than
re-parsing (0.6 s vs 23 s for an 8 k-class app).

Comparing *from* records is currently the library API (a CLI `compare`/`score` is on
the roadmap — see `plans/batch-scoring-design.md`):

```python
from twinflame import load_record, diff
a = load_record("a.tfr.json"); b = load_record("b.tfr.json")
matches = diff(list(a.classes), list(b.classes), threshold=0.8)
```

---

## Useful flags

| Flag | Use |
|---|---|
| `-o, --output PATH` | Diff file to write (default `<apk1>__vs__<apk2>.diff.<ext>` in the cwd). |
| `-f, --format {json,csv,xml}` | Diff file format (default `json`). |
| `--no-file` | Print the summary only; write no file. |
| `-s, --select {all,changed,unchanged}` | Emit only-different, only-same, or all classes (default `all`). |
| `-c, --class NAME` | Restrict the diff to classes whose name/path/descriptor contains NAME. |
| `-m, --matches` | Print the raw per-class match list to stdout instead of the change summary. |
| `--components-file PATH` | Also dump the sensitive-superclass / component report to a file. |
| `-p, --package PREFIX` | Restrict the whole diff to one package (scope filter). |
| `-A, --auto-package` | Derive the app package from the lhs manifest and scope to it. |
| `--app-package PREFIX` | Mark app vs library for ranking (repeatable; multi-root apps). Labeling only — does *not* filter scope. |
| `--min-confidence high` | Drop low-confidence `modified` (cross-toolchain call-churn noise). |
| `--no-cluster` | One global pool — for cross-app (UC2) or heavily repackaged builds. |
| `--find-obfuscated` | Route obfuscated-looking packages into a single fallback pool. |
| `--deobfuscation-map OUT` | Emit a `mapping.txt`. |
| `-j, --jobs N` | Parallel workers (defaults to all cores; the diff scales with it). |
| `--keep-boilerplate` | Keep generated `Comparator` twins (skipped by default as review noise). |
| `-t, --threshold T` | Minimum similarity to report a structural match (default 0.8). |

## Performance

Single machine, all cores: a ~40 k-class app (~145 MB APK) loads in ~2 min and diffs in ~1.5
min. Load (androguard parse + xref) dominates; the diff scales with `--jobs`.

## Interpreting confidence at a glance

- Comparing **two builds of your app from the same toolchain** (e.g. adjacent CI builds):
  trust all `modified`.
- Comparing **releases built months apart** (different R8/AGP): add `--min-confidence high` so
  library re-optimization churn doesn't drown the real app changes.
- The verdict rests on *semantic features* (framework calls, strings, types, method structure),
  not raw bytecode — so it does **not** flag cosmetic re-optimization as a change.
