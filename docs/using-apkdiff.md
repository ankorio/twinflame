# Using apkdiff

A task-oriented guide. For *how it works internally* see [`how-it-works.md`](how-it-works.md);
for the machine output format see [`change-report-schema.md`](change-report-schema.md).

apkdiff compares two Android builds at the **DEX/class level** and tells you what changed —
even when R8/ProGuard has renamed everything. It's a CLI that emits data (matches, a change
report, a deobfuscation map); you visualize with your own decompiler.

## Inputs

Each side is an APK, a single `.dex`, a directory of `.dex` files, or an explicit
`--dex1/--dex2` list (for dumped/extracted DEX with no APK):

```sh
apkdiff old.apk new.apk                 # two APKs
apkdiff dump_old/ dump_new/             # two directories of dumped classes*.dex
apkdiff --dex1 a/classes.dex --dex2 b/classes.dex   # explicit lists
```

DEX-only input has no manifest, so pass `--app-package <prefix>` (below) instead of `--auto-package`.

---

## UC1 — what changed between two versions of my app

The main use case. Get a ranked, method-localized change report.

```sh
apkdiff old.apk new.apk \
    --app-package com.myapp \        # your app's package root(s); repeat for multi-root
    --changes \                       # human summary to stdout
    --changes-json changes.json       # machine-readable (the real deliverable)
```

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
apkdiff sample_a.apk sample_b.apk --no-cluster --json pairs.json
```

`pairs.json` is the ranked list of strongly-similar class pairs (the shared-code islands). This
mode is **experimental in v1** — matching works, but the report still assumes a version-diff
framing; treat the high-similarity pairs as the signal.

---

## UC3 — recover original names (deobfuscation)

Produce a ProGuard-style `mapping.txt` that renames the new build's obfuscated classes using
names recovered from a clear-named (older/donor) build:

```sh
apkdiff old.apk new.apk --deobfuscation-map recovered.txt
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

## Useful flags

| Flag | Use |
|---|---|
| `--app-package PREFIX` | Mark app vs library for ranking (repeatable; multi-root apps). Labeling only — does *not* filter scope. |
| `--package PREFIX` | Restrict the whole diff to one package (scope filter). |
| `--changes` / `--changes-json OUT` | Semantic change report (text / JSON). |
| `--min-confidence high` | Drop low-confidence `modified` (cross-toolchain call-churn noise). |
| `--no-cluster` | One global pool — for cross-app (UC2) or heavily repackaged builds. |
| `--deobfuscation-map OUT` | Emit a `mapping.txt`. |
| `--jobs N` | Parallel workers (defaults to all cores; the diff scales with it). |
| `--keep-boilerplate` | Keep generated `Comparator` twins (skipped by default as review noise). |
| `--threshold T` | Minimum similarity to report a structural match (default 0.8). |

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
