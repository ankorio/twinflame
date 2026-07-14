# twinflame

DEX/APK **bytecode-level** diffing engine for Android reverse engineers. Matches classes and methods across two builds by *structure* — surviving R8/ProGuard renaming — and emits a ranked, method-localized **change report** (what was added / removed / modified) plus a ProGuard `mapping.txt` to carry recovered names into your decompiler. Implements the four-stage architecture from Quarkslab's _"Android Application Diffing: Engine Overview"_ (Czayka & Thomas, 2019).

Use cases:

- **Vulnerability patch identification** — find the class a vendor changed to fix a flaw.
- **Repackaging / mod analysis** — find injected code that replaces vendor logic with no-ops or hostile callbacks.
- **Malware triage / kinship** — find shared code across samples and flag when both implement the same permission-gated component (AccessibilityService, NotificationListener, DeviceAdmin). `twinflame family build` distills several samples of one family into a persistent signature of their shared class structure; `twinflame family match` then scores an unknown sample (or a memory dump) against it — cheap enough to gate a prefilter hit without diffing the whole corpus. (`twinflame score` is the older, two-sample-only containment primitive.) Both are experimental/uncalibrated — see `family --help`.
- **Deobfuscation correlation** — pair classes across Proguard-renamed builds.

Inputs can be APKs, single `.dex` files, or directories of dumped `.dex` (memory dumps included). For pipelines that compare a sample against many, `twinflame prepare` fingerprints a sample once into a packed binary record (`.tfr`, ~20× smaller than JSON) that any diff accepts in place of the APK — record-vs-record compares run about 2× faster end-to-end. `twinflame migrate` upgrades a prepared corpus in place across releases where the stale layer is recomputable, so a large record DB doesn't have to be rebuilt from the original samples.

## Documentation

Full documentation lives in the **[project wiki](https://github.com/ankorio/twinflame/wiki)**:

- **[Using Twinflame](https://github.com/ankorio/twinflame/wiki/Using-Twinflame)** — task-oriented guide: the three use cases, reading the change report, feeding the mapping to jadx/retrace.
- **[Change Report Schema](https://github.com/ankorio/twinflame/wiki/Change-Report-Schema)** — the versioned JSON output format (for downstream tooling).
- **[How It Works](https://github.com/ankorio/twinflame/wiki/How-It-Works)** — how the pipeline works internally, with diagrams.
- **[Evaluation Corpus](https://github.com/ankorio/twinflame/wiki/Evaluation-Corpus)** / **[Accuracy Benchmarks](https://github.com/ankorio/twinflame/wiki/Accuracy-Benchmarks)** — the corpora the accuracy work is graded against and the P/R/F1 results. Wall-clock perf numbers stay in-repo: [BENCHMARKS.md](BENCHMARKS.md).

## Pipeline

1. **Multi-DEX merge** — every `classes*.dex` is unioned into one logical view (first-wins on descriptor, matching ART). The loader also records each method's call targets + incoming-xref count and each class's string constants, which feed anchoring.
2. **Stage 1 — clustering.** Classes group into pools keyed by package name; matching pools across the two APKs compare in parallel. Obfuscated packages (Shannon entropy + length heuristic) fall into a single fallback pool.
3. **Stage B — anchoring.** Within each pool, R8-invariant seeds are locked in *before* structural scoring: classes sharing a rare string constant (IDF-weighted) or a distinctive set of `android/*`/`androidx/*`/`java/*`/`kotlin/*` framework calls are paired with high confidence even when SimHash can't tell them apart. Disable with `--no-anchors`.
4. **Stage 2 — bulk SimHash + bucketed LSH.** Each remaining class gets a 128-bit signature built from four 32-bit partial hashes — `cls`, `fld`, `mth`, `code` — over **structure-only** features (no names, no strings). Nearest neighbors are found via Dullien-style bit-permutation LSH instead of all-pairs.
5. **Stage 3 — accurate, method-level comparison.** Top-k Stage-2 neighbors are re-scored at **method granularity**: methods are assigned 1-to-1 within a class pair (abstract-opcode Levenshtein over 13 semantic categories + prototype + xref), yielding per-method `matched / modified / added / deleted` verdicts. The class score is the instruction-weighted roll-up of those verdicts blended with structural features. Greedy 1-to-1 assignment resolves class-level ties.

Optional Redex pre-pass strips junk-instruction obfuscation (`LocalDcePass` + `RegAllocPass`) before loading.

## Installation

### pip (recommended)

twinflame is on [PyPI](https://pypi.org/project/twinflame/). Requires Python ≥ 3.11; runtime
deps (androguard, numpy, rapidfuzz) install automatically.

```sh
pip install --pre twinflame    # --pre needed while the latest release is a beta
twinflame --help                # console entry point is installed
```

(`pipx install --pip-args=--pre twinflame` works too if you prefer an isolated CLI install.)

That's everything for the core tool. Two features need external programs that aren't Python
packages: `--normalize` needs **Redex** on `PATH` (optional; only for junk-instruction
normalization), and applying a recovered `mapping.txt` needs your own decompiler (e.g. **JADX**).

### Optional: native SimHash accelerator (build it yourself)

The PyPI wheel is pure Python and works on its own. For ~8× faster fingerprinting (`prepare`,
`family build`, libsigs labeling), build the optional Rust accelerator from the repo — it is
**not** shipped as a prebuilt binary; you compile it locally:

```sh
git clone https://github.com/ankorio/twinflame && cd twinflame/native
pip install maturin
maturin develop --release        # builds and installs the `twinflame_rs` module
```

`signature.py` imports it automatically when present and falls back to the pure-Python path
(bit-identical output) when it isn't, so it is a performance option, never a requirement.

### Development checkout

```sh
git clone https://github.com/ankorio/twinflame && cd twinflame
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest tests/
```

### Nix flake (reproducible environment)

For a fully-pinned environment (Python + all deps + Redex built from source + JADX), use the
[Nix flake](https://nixos.wiki/wiki/Flakes) — everything is locked in [flake.lock](flake.lock):

```sh
# Install Nix with flakes (Determinate Systems installer is easiest):
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
git clone https://github.com/ankorio/twinflame && cd twinflame
nix develop            # drops you in a shell with twinflame + Redex + JADX
```

## Running

### One-shot CLI via `nix run`

`nix run` builds the tool on demand and invokes it. The first run compiles Redex from source (~5 min); after that everything is cached.

```sh
# diff two APKs, scope to a vendor package, write a JSON report
nix run . -- old.apk new.apk \
    --auto-package \
    --threshold 0.8 \
    -o report.json

# real-world example: vuln/patched pair, Redex-normalized to defeat
# junk-instruction obfuscation, with the obfuscated-package fallback
# so Proguard-renamed packages cluster together for comparison
nix run . -- vuln.apk patched.apk --normalize --find-obfuscated
```

### Inputs: APK, `.dex`, or dumped DEX (no APK)

Each side accepts an APK, a single `.dex`, or a directory of `.dex` files — so
you can diff **dumped/extracted DEX** (e.g. pulled from memory or an unpacked
payload) with no surrounding APK, with no flag: the input kind is detected.
Partial dumps are tolerated — an unparseable or corrupt `.dex` in a directory is
skipped (with a warning) rather than aborting the load. A DEX-only input has no
manifest, so `--auto-package` is unavailable — pass `--app-package <prefix>` for
app-vs-library ranking instead.

```sh
# directory of dumped classes*.dex on each side
twinflame dump_old/ dump_new/ --no-cluster

# write the machine report as CSV instead of the default JSON, only the
# classes that actually differ
twinflame a/ b/ --no-cluster --app-package com.target.app -s changed -f csv -o changes.csv
```

By default, stdout shows a ranked **change summary** (modified / added / removed /
cosmetic / unchanged, app code first) and any sensitive components both builds
share, while the full machine report is written to a file (`-f json`, default;
`csv`/`xml` also available). Pass `-m`/`--matches` for the raw per-class match
list instead:

```
[+] com.acme.foo: Login - Login.java | com.acme.foo: Login - SourceFile -> 0.8523
      ~ validateToken (0.6100)                              # method modified
      - legacyHash                                          # method removed
      + verifyNonce                                         # method added
[-] com.acme.foo: DeletedClass - DeletedClass.java          # in lhs only
[*] com.acme.foo: AddedClass - SourceFile                   # in rhs only
```

Each `[+]` paired line is followed by the methods that actually changed, so you
see *which* method moved the class score. The machine report carries the full
per-method verdict list, plus each paired class's superclass/interfaces and any
sensitive component it implements.

Timings go to stderr; the report to stdout (so you can `| less` or redirect freely).

#### Worked example

Diffing a vuln/patched APK pair (a real Android wallet app, ~8 k classes per side, the new build is Proguard'd):

```sh
$ nix run . -- old.apk new.apk --normalize --find-obfuscated

```

Reading the output:

- The `load:` and `diff:` lines on **stderr** report timings and class counts so you can see what was actually compared after `--normalize` (Redex strips junk instructions on both sides) and post-filtering.
- `[+]` lines are paired matches with their similarity score (`distance < 1.0`). The two `androidx.activity` examples above pair clear-text class names on the left (lhs preserved source files) against the same logical classes on the right (rhs lost source files to `SourceFile` — Proguard's default). Source-file name was dropped, but the structure + bytecode similarity is high (~0.85–0.95).
- `[-]` is a class only present in lhs; `[*]` only in rhs. Counts at the top tell you the global picture (e.g. _11 042 matches: 602 paired below 1.0, 5 027 deleted, 5 664 added_ — the bulk being unmatched is the price of heavy obfuscation; tweak `--find-obfuscated` and `--threshold` to trade quality for coverage).
- Drop the `--normalize` and re-run to see what _uncleaned_ bytecode looks like — if the average paired similarity drops from ~0.9 to ~0.95+ everywhere, that's the fingerprint of junk-instruction obfuscation that Redex defeats.

### Build a standalone binary

```sh
nix build              # produces ./result/bin/twinflame
./result/bin/twinflame --help
```

### Dev shell

For interactive use (REPL, hacking on the code, running tests):

```sh
nix develop            # enter shell with python + redex + jadx on PATH
pytest tests/          # 265 tests, ~5s
python -m twinflame.cli --help
```

### Python API

```python
from twinflame import load, filter, diff

lhs_app = load("app-1.6.1.apk")
rhs_app = load("app-1.6.3.apk")

lhs = filter(lhs_app.classes, {"package_filtering": "com.vendor.app"})
rhs = filter(rhs_app.classes, {"package_filtering": "com.vendor.app"})

matches = diff(lhs, rhs, 0.8, {
    "synthetic_skipping": True,
    "min_inst_size_threshold": 5,
    "top_match_threshold": 3,
})
for m in matches:
    if m.is_paired and m.distance < 1.0:
        print(f"[+] {m.lhs.info} | {m.rhs.info} -> {m.distance:.4f}")
```

### Cross-version deobfuscation map

`--deobfuscation-map OUT` writes a ProGuard-format `mapping.txt` for **apk2** (the obfuscated/stripped build) by propagating class names recovered from the matched classes in **apk1** (the donor build that still carries the DEX `SourceFile` attribute). Load it into JADX/Ghidra and the rename propagates to every reference automatically — twinflame never rewrites the DEX.

```sh
# apk1 = older build that kept SourceFile; apk2 = the one you want to read
twinflame app-old.apk app-new.apk \
    --find-obfuscated --deobfuscation-map app-new.map --map-min-confidence 0.8
```

Load into JADX with `-Prename-mappings.invert=yes`:

```sh
jadx --mappings-path app-new.map -Prename-mappings.format=PROGUARD_FILE -Prename-mappings.invert=yes \
    -d out_deobf app-new.apk
```

`invert=yes` is required: our file follows the standard ProGuard convention (`<original> -> <obfuscated>:`, what `retrace` consumes), but jadx's `PROGUARD_FILE` reader treats the left side as the name *currently in the binary* — the opposite. Without the flag it silently renames nothing (no error). Same applies in jadx-gui: check "Invert" alongside the mapping path.

- Recovers the class **simple name** (`ContextCompat.java` → `x6.q` becomes `x6.ContextCompat`); the obfuscated **package** and inner-class structure are kept (source files carry no package).
- Anchored / exact (`distance == 1.0`) matches are trusted; lower-confidence ones are still emitted but flagged with a `# low-confidence` comment. Tune the floor with `--map-min-confidence`.
- This is the cross-version superpower over single-APK source-file deobfuscation: it names classes in a build that **stripped** `SourceFile`, using the build that didn't. Method/field name propagation is a planned next step.

### Workflow recipes

- **Patch identification.** Scope with `--package PREFIX` or `--auto-package` (drastically shrinks the comparison set), use `--threshold 0.8` since bug fixes are minor changes. Skip `distance == 1.0` matches.
- **Modded / repackaged app.** Drop `--package` (mods often live in bundled SDKs, not the vendor package). If you see a sea of ~0.95 "everything changed" matches, run with `--normalize` — that's the signature of junk-instruction obfuscation.
- **Heavily Proguard'd inputs.** Add `--find-obfuscated` so single-letter package names (`a.b.c`) collapse into a fallback pool rather than failing to pair with their clear-text counterparts.
- **Deobfuscating a stripped build.** Diff it against an older build that kept `SourceFile`, add `--find-obfuscated --deobfuscation-map out.map`, then load `out.map` in JADX.
- **Malware family signature.** Distill several known samples of one family into a reusable pack, then score unknowns (APKs, `.dex`, or memory-dump directories) against it. Unpack packed samples first — a packer hides the payload from structural matching.

  ```sh
  # build a family signature from N samples (records, APKs, .dex, or dumps)
  twinflame family build -o fam.tflp --name evilbot sample1/ sample2/ sample3/
  # score an unknown dump against it (match radius defaults to the pack's build radius)
  twinflame family match fam.tflp suspect_dump/
  # or triage against a whole directory of family packs, ranked
  twinflame family match packs/ suspect_dump/
  ```

### Evaluation harness (`eval/`, plan M3.2)

Grades twinflame's own class matches against real ground truth instead of eyeballing them. Build an OSS Android app **twice** — R8 off, then R8 on at full optimization — the `mapping.txt` R8 writes for the R8-on build is a free, perfect oracle for the renaming-only case (directly grades M1.1 + M1.2; inlining/outlining is a future difficulty-graded slice per M3.1).

```sh
python -m eval.cli app-r8-off.apk app-r8-on.apk app-r8-on/mapping.txt --package com.vendor.app
```

Prints true/false positive & negative class-match counts plus precision/recall/F1. `eval/mapping.py` parses the ProGuard-format oracle (same format `deobf.py` emits); `eval/score.py` exposes `score_class_matches(matches, ground_truth) -> ScoreResult` for use as a library, independent of the CLI. Kept out of the `src/twinflame` package on purpose — it's tooling to grade the engine, not part of it.

## Layout

| Module                                               | Role                                                                 |
| ---------------------------------------------------- | -------------------------------------------------------------------- |
| [src/twinflame/model.py](src/twinflame/model.py)         | Dataclasses: `App`, `Class`, `Method`, `Field`, `Signature`, `Match`, `MethodMatch` |
| [src/twinflame/loader.py](src/twinflame/loader.py)       | androguard wrapping + multi-DEX merge; captures calls/strings/xrefs  |
| [src/twinflame/manifest.py](src/twinflame/manifest.py)   | `AndroidManifest.xml` → `ManifestInfo`; dev-package suggestion       |
| [src/twinflame/normalize.py](src/twinflame/normalize.py) | Redex subprocess wrapper                                             |
| [src/twinflame/cluster.py](src/twinflame/cluster.py)     | Package-based pools + entropy/length obfuscation fallback            |
| [src/twinflame/anchor.py](src/twinflame/anchor.py)       | Stage B: string-IDF + framework-call seed matches                    |
| [src/twinflame/signature.py](src/twinflame/signature.py) | 128-bit SimHash + LSH bucket index                                   |
| [src/twinflame/accurate.py](src/twinflame/accurate.py)   | Abstract opcodes, method-level + class scoring, greedy 1-to-1 assignment |
| [src/twinflame/opcodes.py](src/twinflame/opcodes.py)     | Dalvik opcode → 13-category lookup table                             |
| [src/twinflame/\_hot.py](src/twinflame/_hot.py)          | Hot loops (popcount, Hamming, rapidfuzz-backed Levenshtein)          |
| [src/twinflame/parallel.py](src/twinflame/parallel.py)   | ProcessPool helpers for parallel pool comparison / side loading      |
| [src/twinflame/prepare.py](src/twinflame/prepare.py)     | Packed `.tfr` record codec: `prepare` / `migrate`, per-layer versioning |
| [src/twinflame/propagate.py](src/twinflame/propagate.py) | Type-graph match-propagation cascade over confirmed pairs           |
| [src/twinflame/features.py](src/twinflame/features.py)   | Semantic feature layer feeding change classification                 |
| [src/twinflame/changes.py](src/twinflame/changes.py)     | Change classifier: typed, ranked change report from raw matches      |
| [src/twinflame/provenance.py](src/twinflame/provenance.py) | App vs. bundled-library class discrimination                       |
| [src/twinflame/boilerplate.py](src/twinflame/boilerplate.py) | Generated-boilerplate (structural-twin) detection                |
| [src/twinflame/components.py](src/twinflame/components.py) | Sensitive-component detection (AccessibilityService & co.)         |
| [src/twinflame/score.py](src/twinflame/score.py)         | Tier-1 containment scoring, `twinflame score` (experimental)         |
| [src/twinflame/report.py](src/twinflame/report.py)       | Machine-report serialization (JSON/CSV/XML)                          |
| [src/twinflame/deobf.py](src/twinflame/deobf.py)         | Cross-version deobfuscation: recovered names → ProGuard mapping.txt   |
| [src/twinflame/api.py](src/twinflame/api.py)             | Public `load / filter / diff` entry points                           |
| [src/twinflame/cli.py](src/twinflame/cli.py)             | `twinflame` console script                                             |

## Tests

```sh
nix develop --command pytest tests/
```

Currently: **265 tests** (2 need Redex on PATH and auto-skip without it — the dev shell provides it).

Test fixtures are **pure-synthetic**: `tests/fixtures/synthetic.py` builds `Class` objects directly via Python, no binaries committed to git. The integration tests exercise the full diff pipeline (cluster → anchor → signature → accurate → api) on these objects.

The on-disk DEX/APK round-trip (loader.py end-to-end) currently has a smoke-only test; a real binary DEX emitter under [tests/fixtures/build_dex.py](tests/fixtures/build_dex.py) is stubbed for future work.

## Known limitations

- **Identical-structure classes collide.** Pools where many classes share signatures (trivial getter/setter classes, generated stubs) may produce false pairings via greedy assignment. `--min-instr 5` filters trivial classes; raising `--neighbors` widens the candidate pool.
- **Limited inheritance context.** *Framework* first-level hierarchy (super/interfaces under `java/android/androidx/kotlin/*`) is now used three ways: folded into the SimHash, as a class-similarity feature, and as a rename-invariant anchor that reaches string-less classes (cf. LibPecker, csl-ugent/apkdiff). *App* supertypes — renamed per build — are still only exploited post-match by the type-graph propagation pass (`propagate.py`), not during initial matching.
- **No cross-boundary / optimization resilience yet.** Matching assumes a 1:1 class and method correspondence. R8 *optimizations* that break that assumption — inlining, outlining, class merging — are a planned future track (see `twinflame-next-dev-plan.md`, M3.1), not yet implemented.
- **JNI / native-code changes are invisible.** The diff is purely Dalvik-level — native library mutations require a complementary native-code diff.
- **LSH is approximate by design.** `--buckets N` tunes the accuracy/speed knob.

## License

MIT.
