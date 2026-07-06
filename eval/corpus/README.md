# Evaluation corpora

twinflame's accuracy work is graded against real corpora, never eyeballed. This directory
documents how each corpus is produced. **No APKs or DEX are committed** (binaries stay
out of git); only the reproducible recipe lives here. Build outputs go to a working area
**outside** the git repo — the convention is a `corpus/` directory alongside the
repo checkout (`../corpus/` relative to this repo root), which is not itself a git repo.

## What each corpus grades

| Corpus | Kind | Oracle | Grades (use case) |
|---|---|---|---|
| CalculatorM3 | real R8 double-build | `mapping.txt` | rename recovery (UC3), method matching |
| Fossify Contacts | built here (below) | `mapping.txt` | rename recovery (UC3) — **second corpus** |
| BlackyHawky Clock | built here (below) | `mapping.txt` | rename recovery (UC3) |
| OpenCamera | built here (below) | `mapping.txt` | rename recovery (UC3) |
| private benchmark app (2 releases) | supplied binaries | none (unlabeled) | version change (UC1) — spot-check only |

The buildable OSS apps are the important unlock: because we control the build we get a
`mapping.txt` oracle for free, **and** we can rebuild the same source to measure the
change-detection *noise floor* (see `../../plans/change-detection-roadmap.md`, E-2).

## Benchmark snapshot (2026-07-05, `--assignment auto`, boilerplate filter on)

Full-suite run across every built corpus. Details + methodology for each are in the
sections below; this is the at-a-glance scoreboard.

| Corpus | Setup | Oracle | Precision | Recall | F1 |
|---|---|---|---|---|---|
| Contacts 1.5.0→1.6.0 | release-vs-release (UC1) | mapping-join | 1.000 | 0.949 | 0.974 |
| Contacts 1.4.0→1.6.0 | release-vs-release (UC1) | mapping-join | 1.000 | 0.948 | 0.973 |
| Contacts 1.3.0→1.6.0 | release-vs-release, **major span** | mapping-join | 0.986 | 0.935 | 0.960 |
| Clock 2.30→2.31 | release-vs-release (UC1) | mapping-join | 0.996 | 0.922 | 0.957 |
| Clock rebuild a vs b | **same source ×2 (E-2 noise floor)** | mapping-join | **1.000** | 0.923 | 0.960 |
| CalculatorM3 | debug↔release (UC3 rename) | mapping.txt | 0.931 | 0.677 | 0.784 |
| Clock optimize↔strict | cross-obfuscation (layout) | mapping-join | 1.000 | 0.923 | 0.960 |
| Clock lax↔optimize | cross-obfuscation (**optimization gap**) | mapping-join | 0.982 | 0.619 | 0.759 |

**Reading it:**
- **UC1 release-vs-release is the headline: P≈1.0, R 0.92–0.95** on two independent apps,
  holding across a major (3-release) span.
- **E-2 noise floor:** on *identical* source, precision is **perfect** (no false matches) but
  recall tops out ~0.92 — a residual structural-twin ceiling the filter can't reach. (This is
  the *matching* floor; the *change-classifier* floor is V1-B, still to calibrate.)
- **CalculatorM3 (debug↔release)** is the pessimistic bound — it conflates rename with a large
  optimization gap the real UCs don't have (roadmap: ~3× harder), so R 0.68 is expected.
- **Obfuscation axis:** layout obfuscation (repackage/access-mod) is matcher-invariant (F 0.96);
  the **optimization gap** (lax↔optimize inline/merge) is the real difficulty (F 0.76) — the
  regime aggressively-optimized apps (e.g. Telegram) fall into.

Two runs have no P/R oracle (org-released binaries ship no `mapping.txt`) and are assessed by
running the `--changes` report + spot-check:
- **Private benchmark app** (cartera 1.8.2→1.9.1, ~2.5-mo cross-toolchain gap): runs; report =
  `modified 1276 / added 5012 / removed 5008 / cosmetic 286 / unchanged 277`. The large symmetric
  add/remove is library churn + toolchain drift — what `--app-package` + the V1-B noise floor tame.
- **Telegram 12.7.2→12.8.3** (~145 MB, ~40 k classes/side): see the "Large real-world app" note below.

### Large real-world app — Telegram 12.7.2 → 12.8.3 (scale + qualitative)

Official APKPure builds (no `mapping.txt`), the biggest target run to date. Validates that the
pipeline scales and that the change report stays reviewable on a real, adjacent-version diff.

- **Scale/perf:** 39,108 → 40,090 classes/side, 145 MB APKs. **Load 123 s + diff 94 s ≈ 3.5 min**
  total (`--jobs` = all cores; 517 pools compared, largest 2457×2467). 21,723 matches.
- **Change report (`--changes --app-package org.telegram`):**
  `modified 394 / added 888 / removed 295 / cosmetic 497 / unchanged 19,649`; **app-only**
  `modified 384 / added 282 / removed 135 / cosmetic 414 / unchanged 10,436`. i.e. of ~40 k
  classes the reviewer is handed **~384 app classes to look at** — the rest are unchanged/
  library/cosmetic. That triage ratio is the whole point of the tool.
- **Localization on real code is directly actionable** (Telegram keeps app class/method names,
  only strips `SourceFile`): e.g. `TelegramMediaSession.<init> +18/-8 calls`,
  `ArticleViewer.checkLayoutForLinks +15 calls`, `applyQueueFor +7/-8 calls`. Anchoring held
  across the version bump (`[anchored]` matches).
- **Caveat:** Telegram's app code isn't name-obfuscated, so this exercises *scale* and the
  *change classifier*, not rename recovery. There's no P/R here (no oracle) — building from
  source at two tags would add one.

## Build recipe (rename-recovery corpus: one version, R8-off vs R8-on)

The existing scorer (`python -m eval.cli donor.apk target.apk mapping.txt`) wants a
**donor** (R8-off, clear names) and a **target** (R8-on, obfuscated) of the *same*
source, plus the target's real `mapping.txt`. A debug/release pair produces exactly this.

Prereqs: JDK 17+ (`JAVA_HOME`), Android SDK (`ANDROID_HOME`) with a platform + build-tools
the app targets. Each project ships a Gradle wrapper (`./gradlew`) — no system Gradle needed.

### Fossify Contacts (Kotlin, AGP/Gradle 9.x, flavors core/foss/gplay)
```bash
git clone https://github.com/FossifyOrg/Contacts.git && cd Contacts
git checkout <tag>                       # e.g. a release tag; omit for main
echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew --no-daemon :app:assembleFossRelease :app:assembleFossDebug
# donor  (clear):  app/build/outputs/apk/foss/debug/app-foss-debug.apk
# target (obf):    app/build/outputs/apk/foss/release/app-foss-release-unsigned.apk
# oracle:          app/build/outputs/mapping/fossRelease/mapping.txt
```
Release falls back to **unsigned** with no keystore (fine — we only read DEX). Uses
`proguard-android-optimize.txt` (full R8: shrink + optimize/inline + rename).

### BlackyHawky Clock — https://github.com/BlackyHawky/Clock
### OpenCamera — https://sourceforge.net/p/opencamera/code/ (git mirror / SF checkout)
Same shape: `./gradlew assembleRelease assembleDebug`, adjust flavor task names per
project (inspect `app/build.gradle*` `buildTypes`/`productFlavors` first; confirm release
has `minifyEnabled=true` — if not, add a `minifyEnabled=true` release-copy variant).

## Baselines on record (rename recovery, current `main`, `--assignment auto`)

Measured 2026-07-03 with `python -m eval.cli donor target mapping --package <prefix>`.
These are the pre-improvement baselines every Phase 0 accuracy fix is graded against.

| Corpus | UI stack | Ground truth | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|
| CalculatorM3 (`--package com.vagujhelyigergely.calculatorm3`) | Compose | 155 | 62 | 10 | 37 | 0.861 | 0.626 | 0.725 |
| Fossify Contacts (`--package org.fossify`) | Compose | 320 | 83 | 5 | 237 | 0.943 | 0.259 | 0.407 |
| BlackyHawky Clock (`--package com.best.deskclock`) | Java/Views | 286 | 159 | 6 | 127 | 0.964 | 0.556 | 0.705 |

All three use `proguard-android-optimize.txt` (aggressive R8: shrink + optimize/inline +
rename), so the "renaming-only slice" the harness docstring claims is actually
rename+optimization — debug (unoptimized) vs release (optimized) diverge structurally even
for identical source. Keep that in mind: this corpus set tests optimization-resilience too,
not pure renaming. A clean renaming-only slice would need minify-off-vs-on at the *same*
optimization level (not built).

### Diagnostic (Fossify Contacts, the worst case) — the recall gap is candidate generation

Instrumented the pipeline against ground truth to locate *where* recall is lost:
- **Horizontal class merges: 0.** Every one of the 320 ground-truth classes has a distinct
  1:1 target — so the FN are not structurally-impossible merges (rules out that M3.1 cause).
- Of the 273 classes with both sides present in the loaded APKs (47 had one side absent —
  shrunk or loader-filtered):
  - **84% (232): the correct target is never surfaced by LSH** (not in the top-k neighbor
    set). Candidate generation is the overwhelming failure mode.
  - Of the 41 that do surface, only 21 are the top-1 neighbor and only **7 would pass the
    0.8 similarity threshold** — so even when surfaced, `class_similarity` scores true
    pairs low (debug's unoptimized bytecode vs release's R8-inlined bytecode drift the
    opcode-Levenshtein and the method/field-count features).

**Consequences for the plan.** Two compounding upstream failures, both before assignment:
1. **Signature quality dominates.** 84% of true pairs aren't even near-neighbors, which
   means multi-probe LSH (probing adjacent buckets, Phase 0.1) is *necessary but not
   sufficient* — if true pairs have genuinely dissimilar signatures it can't reach them.
   The higher-leverage fix is **signature diversification (Phase 0.2)**: add the
   rename-invariant, optimization-robust tokens (superclass, interfaces, framework-call
   targets, referenced types) so true pairs get *similar* signatures in the first place.
   Reorder Phase 0 to do 0.2 before/with 0.1.
2. **Scoring drifts under optimization.** Even surfaced pairs mostly score < 0.8 because
   the structural/bytecode signature is perturbed by R8 inlining. Argues for the
   order-invariant call/type-multiset similarity (roadmap semantic-feature layer) as a
   scoring input, not just linear opcode-Levenshtein.

**Three-corpus takeaway:** precision is uniformly high (0.86–0.96 — matches made are
reliable); **recall is the universal gap** (0.26–0.63) and is dominated by candidate
generation. This is now backed by three corpora, not one.

### After Phase 0.2 — signature diversification (framework super/iface/call tokens)

Added rename-/optimization-invariant tokens to the SimHash (`signature.py`: framework
superclass + interface tokens in `_class_features`, framework call-target tokens in
`_code_features`). Measured:

| Corpus | Precision | Recall | F1 | vs baseline |
|---|---|---|---|---|
| CalculatorM3 | 0.943 | 0.667 | 0.781 | **P +0.082, R +0.041, F1 +0.056** |
| Fossify Contacts | 0.977 | 0.259 | 0.410 | P +0.034, R flat, F1 +0.003 |
| BlackyHawky Clock | 0.963 | 0.545 | 0.696 | P flat, R −0.011 (≈3 classes, noise) |

Net positive (clear CalculatorM3 win; precision up or flat everywhere; no real
regression), so the change stays. But recall is flat on the hard corpora, and two
diagnostics pin down why — both *downstream* of the signature:

1. **Candidate generation has a real ceiling on Contacts, not just an LSH-bucketing
   problem.** Brute-force nearest-neighbor by Hamming (no LSH): the correct target is the
   single nearest for only 20% of true pairs, top-3 for 26%, top-20 for 48% (median rank
   25, mean 207, max 4314). So even a perfect neighbor search misses most pairs — debug
   (unoptimized) vs release (R8-inlined) diverge too far for a structure+call signature to
   co-locate them. Multi-probe LSH (Phase 0.1) recovers only the LSH-vs-brute gap (~33%→48%
   at k=20) — worth doing, but secondary. Raising `k` saturates fast (k=50 → 39% surfaced).
2. **The scoring wall is now the binding constraint.** Of 59 surfaced Contacts pairs, only
   8 would pass the 0.8 `class_similarity` threshold — R8-inlined bytecode tanks the
   opcode-Levenshtein term, so surfaced true pairs are *rejected by the scorer*. Signature
   quality confirmed improved (Contacts top-1 candidate ranking 21→38), but it can't move
   recall while scoring rejects the candidates.

**Signature diversification stays** (kept in `signature.py`). The framework super/iface/call
tokens are net-positive: clear CalculatorM3 win, precision up or flat everywhere, ranking
sharper. Recall on the hard corpora is bounded by the two downstream limits above.

### Phase 0.4 — framework-call term in the *score*: tried, measured, REVERTED

Hypothesis: inject framework-call agreement into `class_similarity`/`compare_classes` (blend
0.5 bytecode / 0.3 features / 0.2 call-Jaccard, only when calls exist) so inlining-divergent
true pairs clear the 0.8 threshold. Measured across all three corpora:

| Corpus | F1 sig-only | F1 + score-blend | Δ |
|---|---|---|---|
| CalculatorM3 | 0.781 | 0.786 | +0.005 |
| Fossify Contacts | 0.410 | 0.402 | −0.008 |
| BlackyHawky Clock | 0.696 | 0.674 | −0.022 |

**Net negative — reverted.** Direct check: of 59 surfaced Contacts pairs, 7 cleared 0.8
(was 8); median surfaced-pair score 0.52, nowhere near 0.8. The 0.2 call weight is far too
small to lift inlining-tanked pairs, and rebalancing 0.6/0.4→0.5/0.3/0.2 *lowered* scores
wherever call sets disagreed.

**Why it disagrees — the load-bearing lesson: framework-call sets are NOT invariant under
R8 inlining.** Inlining *relocates* calls from an inlined callee up into its caller (and
strips the callee), so both the caller's and the host's call sets change between builds. So
call-Jaccard is a *noisy* signal precisely in the inlining-heavy scenario it was meant to
rescue — good as additive bucketing evidence in the signature (tolerant, measurably helped
ranking + precision), bad as a hard scoring term. A future scoring rescue needs either
IDF-weighted calls (only rare shared calls count) or the order-invariant semantic-feature
layer — not a naive Jaccard blend.

**Bigger realization (drives the next corpus):** debug-vs-release is an *unrepresentative*
test. Debug is unoptimized; release is heavily R8-inlined — so this measures
optimization-resilience, which the real use cases don't face: UC1 compares two *release*
versions and UC2 two *release* malware samples, both similarly optimized, so true pairs stay
structurally close and the scoring wall largely vanishes. Next: build a **release-vs-release**
corpus (two adjacent tags, or the same tag twice = also the E-2 noise-floor measurement) to
measure the matcher without the artificial optimization gap.

## Release-vs-release results (2026-07-03) — the representative test

Built with `eval/xversion.py` (grades two obfuscated release builds by joining their
`mapping.txt` files on original class name → ground-truth obf↔obf pairs; added/removed
originals reported separately as the change signal). This removes the debug-vs-release
optimization gap and matches the real use cases (UC1 two versions; UC2 two samples).

### Pure build-noise floor (E-2): **zero.** R8 is deterministic
Clock 2.31 built twice (clean between) → **byte-identical APKs** (same md5, identical
`mapping.txt`). So two builds of identical source produce identical output: **any
difference between two release builds is a real source change, not build noise.** Strong
result for change detection — no build-noise false-positive budget needed for reproducible
builds like this.

### Matcher intrinsic ceiling ~0.86 on identical input — root cause (twins were a red herring)
Scoring the byte-identical Clock pair: **P 1.000, R 0.860** — 40 of 286 misses even against an
exact copy. The first read blamed structural twins; **categorizing the 40 actual FN disproved
that:**
- **23 — filtered by `min_inst_size_threshold=5` before matching** (tiny <5-instruction classes
  never reach assignment).
- **16 — `R8$$REMOVED$$CLASS$$…` phantoms**: classes R8 *deleted* by dead-code shrinking but
  left a placeholder for in `mapping.txt`. They aren't in the APK → uncountable ground truth.
- **1 — an actual structural-twin mispair.**

So "twins" were ~2.5% of the gap, not the story. The two real causes are an **oracle bug** and an
**over-aggressive filter** — both fixed below. (The earlier "25 signature-colliding targets"
figure conflated *having* a colliding signature with *failing because of it*; a filtered class
never reaches the collision.)

### Fixes applied this pass

**1. Oracle: exclude `R8$$REMOVED` phantoms** (`eval/mapping.py::is_removed_target`, wired into
both scorers). Corpus-dependent inflation: Clock 16 (5%), Contacts 2, CalculatorM3 0. Pure
measurement correctness — the matcher was always this good.

**2. Matcher: `min_inst_size_threshold` 5 → 2** (`model.py`). The 5 default dropped small-but-real
classes; measured sweep shows lowering it improves F1 on every corpus. `2` keeps everything with a
real body while still skipping empty 0/1-instruction shells. Final numbers (both fixes applied):

| Corpus | before (thr5, phantom oracle) | after (thr2, fixed oracle) |
|---|---|---|
| CalculatorM3 (debug-vs-release) | P .943 R .667 F1 .781 | P .931 R .677 F1 **.784** |
| Clock 2.30→2.31 (release-vs-release) | P .996 R .857 F1 .921 | P .996 R .932 F1 **.963** |
| Contacts 1.5.0→1.6.0 (release-vs-release) | P 1.00 R .784 F1 .879 | P 1.00 R .840 F1 **.913** |

Release-vs-release recall gains 5–8 points at ~zero precision cost (Contacts stays a perfect
1.000). The genuine structural-twin case remains (~1–2% of classes, needs identity anchors:
strings / type-graph position) but is now correctly sized as a minor, precision-safe residual —
not the main lever it was mistaken for.

### Cross-version matching (real UC1): recall roughly triples vs debug-vs-release

| Comparison | Gradable | +added / −removed | Precision | Recall | F1 |
|---|---|---|---|---|---|
| Contacts debug-vs-release (same version) | 320 | — | 0.977 | 0.259 | 0.410 |
| **Contacts 1.5.0 → 1.6.0 (release-vs-release)** | 594 | +2 / −0 | **1.000** | **0.784** | **0.879** |
| Clock debug-vs-release (same version) | 286 | — | 0.963 | 0.545 | 0.696 |
| **Clock 2.30 → 2.31 (release-vs-release)** | 280 | +6 / −3 | **0.996** | **0.857** | **0.921** |
| Clock 2.31 vs 2.31 (identical — ceiling) | 286 | +0 / −0 | 1.000 | 0.860 | 0.925 |

The debug-vs-release harness overstated difficulty ~3× (Contacts) / ~1.5× (Clock) on recall.
On the real release-vs-release setup **precision is near-perfect (0–1 mispairings)** and recall
jumps to 0.78–0.86. Note **Clock's cross-version recall (0.857) essentially equals its
identical-input ceiling (0.860)** — a genuine minor version change costs almost nothing; the
matcher is running at its structural limit, and the residual gap is structural twins + loader
drops, not version difficulty. Change signals are small and clean (a handful of add/remove per
minor version), exactly what UC1 wants.

**Strategic consequence.** (1) Grade UC1/UC2 work on release-vs-release, not debug-vs-release
— the latter conflates a large optimization gap the real cases don't have. (2) The recall
gap that remains is now *precision-safe* to attack (0 FP headroom): multi-probe LSH + the
structural-twin/identity-anchor problem are the levers, not the scoring wall (which was a
debug-vs-release artifact). (3) Change detection's build-noise floor is zero for reproducible
builds — the real "noise" is R8 re-optimization between *different* toolchain versions, still
to be measured (two AGP versions of the same source).

### Multi-probe LSH (Phase 0.1) — implemented, correct, defaulted OFF (no end-to-end gain at 16 perms)

Added `probe_radius` to `LSHIndex` (probe Hamming-1/2 prefix neighbors per permutation) +
`DiffOptions.probe_radius`. Measured:

| Config | Effect |
|---|---|
| Candidate surfacing, Contacts, n_perm=16 | radius 0→1: 248→257 pairs (+2pts). Works. |
| End-to-end recall, Clock 2.30→2.31 (rel-rel) | radius 0/1/2 **identical** (P .996 R .932) |
| End-to-end recall, CalculatorM3 (dbg-rel) | radius 0/1/2 **identical** (P .931 R .677) |
| Candidate surfacing at n_perm=4 vs 16 | 31% vs 41% — surfacing is permutation-count-bound |

**Conclusion: no end-to-end benefit at the default 16 permutations, so defaulted OFF (radius 0).**
Why: 16 exact-bucket probes already saturate near-neighbor recall (a small-Hamming neighbor keeps
its differing bits out of the 16-bit prefix in *some* permutation with high probability), so radius-1
is largely redundant. The +2pts it does surface on debug-vs-release don't convert — they're blocked
by the 0.8 score threshold; on release-vs-release the true pairs are already in the exact bucket.
Multi-probe's genuine use is recovering recall when permutations are *few* (a memory/perf tradeoff),
so the capability is kept and tested, just off by default. The real recall levers remain: the
`min_inst`/oracle fixes (landed) and, for the residual, identity anchors for structural twins.

## Obfuscation profiles — strength axis & cross-obfuscation matrix

The harness can now build one app version under multiple obfuscation profiles and grade the
matcher across them (`profiles/` snippets + `build_profiles.sh` + `eval/matrix.py`; see
`profiles/README.md`). Profiles: **lax** (`-dontoptimize`, rename+shrink only), **optimize**
(app default, inlining on), **strict** (`-allowaccessmodification -repackageclasses ''`,
full package flattening). ProGuard-engine is a documented harness-ready slot (AGP 8 has no
built-in ProGuard; needs the standalone plugin/CLI).

### Result — Clock 2.31 profile matrix (F1, cross-obfuscation join on original name)

Class counts show the profiles are real: **lax 1074 → optimize 423** app classes (optimization
inlined/merged away ~60%); strict flattened 354 classes to the root package (verified).

|  X ↓ / Y → | lax | optimize | strict |
|---|---|---|---|
| **lax** | — | 0.745 | 0.745 |
| **optimize** | 0.705 | — | **0.966** |
| **strict** | 0.705 | **0.966** | — |

Two clean, load-bearing findings:

1. **Layout obfuscation is matcher-invariant.** optimize↔strict — which adds full package
   repackaging + access modification — holds at **P 1.000, R 0.933** (≈ the same-code ceiling).
   The matcher is global-pool structure-based, so package flattening/renaming doesn't touch it.
   Directly reassuring for **UC2** (malware repackaging): scrambling names/packages doesn't defeat
   matching.
2. **The optimization gap (inlining/merging) is the real difficulty**, not renaming/repackaging.
   lax↔optimize drops to **R 0.60** because ~60% of lax's classes were inlined/merged away in
   optimize, so they have no 1:1 counterpart. This is the same difficulty axis as debug-vs-release,
   now isolated cleanly: it's *optimization*, not *obfuscation strength per se*, that hurts.

Also surfaced — a **directional FP asymmetry**: querying the smaller optimized set *into* the
larger un-optimized set inflates false positives (optimize→lax 31 FP / P .841) vs the reverse
(lax→optimize 3 FP / P .982) — more decoy candidates on the larger side. Worth remembering when
choosing which build is lhs vs rhs.

**Takeaway for the tool:** aggressive *obfuscation* (rename/repackage/access-mod) is a solved
case; aggressive *optimization* (inline/merge) is the open one — which is the M3.1 territory, now
measurable on demand across the strength axis instead of guessed at.

## Version-change corpus (UC1) — cross-version matching over a widening gap

`eval/corpus/build_xversion.sh <repo> <tagA> <tagB> assembleFossRelease <out> [gradle_ver]`
builds two release tags (both R8-on) and stages `app.apk`+`mapping.txt`; `eval.xversion`
grades matching by joining the two `mapping.txt`s on original class name (no connecting git
history needed — shallow clones work). `--package org.fossify.contacts` scopes to app code.

### Fossify Contacts degradation curve (2026-07-05), all vs 1.6.0, `--package org.fossify.contacts`

Two numbers per row: **before** the boilerplate filter (grading *all* classes incl. generated
twins) → **after** (V1-A: generated boilerplate excluded from the oracle, and the matcher skips
tiny `Comparator` twins). The "after" is the honest real-app-logic accuracy.

| Older tag → 1.6.0 | Releases | commons | Precision | Recall | F1 | (after: gradable) |
|---|---|---|---|---|---|---|
| 1.5.0 | 1 (adjacent) | 5.12 → 6.1 | 1.000 → **1.000** | 0.901 → **0.949** | 0.948 → **0.974** | 78 |
| 1.4.0 | 2 | 5.7 → 6.1 | 1.000 → **1.000** | 0.867 → **0.948** | 0.929 → **0.973** | 77 |
| 1.3.0 | 3 (**major**, ~4 mo) | 5.3 → 6.1 | 0.924 → **0.986** | 0.813 → **0.935** | 0.865 → **0.960** | 77 |

**Before the filter, the errors were almost entirely generated-boilerplate twins, not app
logic.** Characterizing the unfiltered 1.3.0→1.6.0 misses (10 FP / 28 FN): Kotlin comparator
lambdas (`$$inlined$sortedBy$N` — structurally identical `Comparator`s), ViewBinding classes
(`ItemEditGroupBinding` ↔ `ItemEditEmailBinding`), and `R$id`/`R$string`/… resource classes
(all-static-int, maximally ambiguous). Only ~1–2 were real-logic classes.

**After the filter (V1-A landed 2026-07-05):** the curve is **nearly flat** — F1 stays ≥0.96
even across the major span, and precision is ≥0.99 everywhere. The residual 1 FP / 4–5 FN are
genuine cross-version refactors of real classes (irreducible version-change signal), not tool
error. Filter is two-sided: `twinflame/boilerplate.py` skips tiny `java/util/Comparator`
implementers in the matcher (obfuscation-robust; R8 keeps the framework interface), and
`eval/mapping.is_generated_boilerplate` excludes `R`/`R$*`, `*Binding`, and `$$inlined$sort*`
by original name from the oracle. Disable the matcher side with `--keep-boilerplate`.
*Known limitation:* in a fully-obfuscated build R8 strips the ViewBinding interface and rewrites
`SourceFile`, so `*Binding`/`R` twins aren't structurally detectable by the matcher (only the
oracle excludes them, by name) — a field-type-profile heuristic could catch them later.

**Build note:** tags ≤ 1.2.0 (commons ≤ 3.0.0) no longer build — a dead transitive jitpack
artifact (`com.github.duolingo:rtl-viewpager:940f12724f`, 404 everywhere); dropped in commons
> 3.0.0. Old wrappers (Gradle 8.2.1) are bumped to 8.13 to run under JDK 21 (AGP 8.2.1 still runs).

### Change-detection oracle (synthetic, DONE) vs. two-tag realism

Ground-truth for the *classifier* (not just matching) is the synthetic-mutation oracle —
`tests/fixtures/mutate.py` + `tests/integration/test_change_oracle.py` (see roadmap E-1). The
two-tag corpus above adds *realism* for the matcher; per-touched-class change ground truth from
the git diff between tags is still open (needs a non-shallow clone).

## Noise-floor calibration (E-2, V1-B) — DONE 2026-07-05

The change classifier's `modified` verdict must not fire on *build* differences. Measured the
noise floor by running `--changes` on **same-source** pairs (any `modified` = pure noise):

| Same-source pair | What differs | modified (all noise) |
|---|---|---|
| Clock rebuild a vs b | nothing (same toolchain) | **0** (R8 deterministic → byte-identical APKs) |
| Clock optimize vs strict | layout obfuscation (repackage/access-mod) | **0** |
| Contacts 1.5.0 @ AGP 8.11.1 vs 8.10.1 | **R8 version** (both optimized) | 450 — but **0 in app code** |
| Clock lax vs optimize | optimization on↔off | 1134 (unrealistic extreme; nobody ships `-dontoptimize`) |

**The cross-R8-version noise (the realistic cross-toolchain case) has two clean signatures:**
by **origin** it is 0 app / 331 library / 119 unknown (the app's own code is stable); by
**delta type** it is **95% call-only framework-call churn** (different R8 versions relocate
calls between classes via inlining — the Phase 0.4 effect), with 0 method-structural and only
5% string/type. Magnitude does *not* separate noise from signal (median 4, up to 50; and 51% of
*confirmed-real* changes are magnitude ≤2 — verified on Telegram against the source diff).

**Mechanism (implemented):** `ClassChange.confidence` = `"low"` iff a `modified` is *call-only
churn in non-app code*, else `"high"`; emitted in JSON, ranked high-first, filterable with
`--min-confidence high` (default `low` = emit everything, "demote never drop"). Validated:
`--min-confidence high` cuts the cross-R8 noise **450 → 23** (95% removed), keeps **100%** of the
source-verified Telegram real changes (all app-origin), leaves same-source rebuilds at 0
modified, and keeps the synthetic oracle at 9/9. A magnitude threshold was rejected (drops the
many real small changes).
