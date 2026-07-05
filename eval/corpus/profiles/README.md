# Obfuscation profiles

The bench harness grades the matcher across an **obfuscation-strength axis** and (where
feasible) different **obfuscation engines**, so we know how recall/precision hold as R8/
ProGuard get more aggressive — not just at one fixed config. A *profile* is applied by
appending `<name>.pro` here to the app's `app/proguard-rules.pro` (and, if present,
`<name>.gradle.properties` to `gradle.properties`) for a clean release build, then
reverting. No edits to the app's own gradle logic; works for any app that references
`proguard-rules.pro` (Clock, Contacts, OpenCamera). Driver: `../build_profiles.sh`.

## Strength axis (R8, the default engine)

| Profile | Rules appended | What it exercises |
|---|---|---|
| **lax** | `-dontoptimize` | Renaming + shrinking only, no inlining/merging. The tier anchoring + structure matching is built for. |
| **optimize** | *(none — app's shipped `-optimize` config)* | Standard aggressive R8: inlining on. What the main corpus measures. |
| **strict** | `-allowaccessmodification`, `-repackageclasses ''` (+ ProGuard-only opt knobs, ignored by R8) | Cross-visibility inlining/merging + full package flattening. Closest free R8 gets to a commercial obfuscator; hardest tier for structure matching. |

R8 honours `-allowaccessmodification` and `-repackageclasses`; it ignores the classic
ProGuard optimization directives (`-optimizationpasses`, `-overloadaggressively`,
`-mergeinterfacesaggressively`) — those bite only under the `proguard` engine below.

## Engine axis

- **R8 full mode** (AGP default) vs **compat mode**: drop a `compat.gradle.properties`
  containing `android.enableR8.fullMode=false` to build the ProGuard-semantics-compatible,
  less-aggressive R8. Cheap way to vary engine behaviour without leaving AGP.
- **ProGuard** (the classic, pre-R8 engine — the "ProGuard vs R8" comparison): modern
  AGP (8.x) has **no built-in ProGuard** (R8 replaced it), so this needs the standalone
  **`com.guardsquare:proguard-gradle`** plugin wired into the app, or ProGuard's CLI run
  over the app's dex with the full library classpath. Both are version-sensitive and
  per-app; treated as a documented, harness-ready slot (a `proguard` profile whose rules
  are the `strict.pro` set, which ProGuard fully honours) rather than an executed default.
  When wired, grade it exactly like the R8 profiles — same `mapping.txt` oracle, same
  `eval/matrix.py`.

## How the profiles are graded

Every profile build emits `app.apk` + `mapping.txt`. Two views:

1. **Cross-profile matrix** (`python -m eval.matrix <profiles_dir> --package PREFIX`):
   grades every profile pair by joining their mappings on the *original* class name
   (same source → full obf↔obf oracle). Measures whether the matcher aligns the *same
   code obfuscated two different ways* — the **UC2 "same code, different obfuscation"**
   case (malware family across obfuscators/strengths) and the strength-divergence curve.
2. **Rename-recovery per profile** (`python -m eval.cli debug.apk <profile>.apk <profile>/mapping.txt`):
   a clear-named debug donor vs each profile target — the recall/precision **degradation
   curve** as obfuscation strengthens.

## Building a profile set

```bash
git -C corpus/Clock checkout -f 2.31
eval/corpus/build_profiles.sh corpus/Clock :app:assembleRelease \
  'app/build/outputs/apk/release/*.apk' \
  app/build/outputs/mapping/release/mapping.txt \
  corpus/profiles-out/clock-2.31  lax optimize strict
python -m eval.matrix corpus/profiles-out/clock-2.31 --package com.best.deskclock
```
Adjust the assemble task / apk glob / package per app (Contacts uses
`:app:assembleFossRelease`, `apk/foss/release/*.apk`, `org.fossify`).
