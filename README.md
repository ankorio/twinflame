# apkdiff

DEX-level Android APK class-diffing engine. Given two APK versions, emits a ranked list of class matches with a normalized similarity score so you can isolate exactly which classes mutated between releases. Implements the four-stage architecture from Quarkslab's _"Android Application Diffing: Engine Overview"_ (Czayka & Thomas, 2019).

Use cases:

- **Vulnerability patch identification** — find the class a vendor changed to fix a flaw.
- **Repackaging / mod analysis** — find injected code that replaces vendor logic with no-ops or hostile callbacks.
- **Deobfuscation correlation** — pair classes across Proguard-renamed builds.

## Pipeline

1. **Multi-DEX merge** — every `classes*.dex` is unioned into one logical view (first-wins on descriptor, matching ART). The loader also records each method's call targets + incoming-xref count and each class's string constants, which feed anchoring.
2. **Stage 1 — clustering.** Classes group into pools keyed by package name; matching pools across the two APKs compare in parallel. Obfuscated packages (Shannon entropy + length heuristic) fall into a single fallback pool.
3. **Stage B — anchoring.** Within each pool, R8-invariant seeds are locked in *before* structural scoring: classes sharing a rare string constant (IDF-weighted) or a distinctive set of `android/*`/`androidx/*`/`java/*`/`kotlin/*` framework calls are paired with high confidence even when SimHash can't tell them apart. Disable with `--no-anchors`.
4. **Stage 2 — bulk SimHash + bucketed LSH.** Each remaining class gets a 128-bit signature built from four 32-bit partial hashes — `cls`, `fld`, `mth`, `code` — over **structure-only** features (no names, no strings). Nearest neighbors are found via Dullien-style bit-permutation LSH instead of all-pairs.
5. **Stage 3 — accurate, method-level comparison.** Top-k Stage-2 neighbors are re-scored at **method granularity**: methods are assigned 1-to-1 within a class pair (abstract-opcode Levenshtein over 13 semantic categories + prototype + xref), yielding per-method `matched / modified / added / deleted` verdicts. The class score is the instruction-weighted roll-up of those verdicts blended with structural features. Greedy 1-to-1 assignment resolves class-level ties.

Optional Redex pre-pass strips junk-instruction obfuscation (`LocalDcePass` + `RegAllocPass`) before loading.

## Installation

apkdiff is distributed as a [Nix flake](https://nixos.wiki/wiki/Flakes). All runtime dependencies — Python, androguard, numpy, Redex (built from source), JADX — are pinned in [flake.lock](flake.lock), so a clean checkout reproduces an identical environment.

**1. Install Nix** (skip if you already have it with flakes enabled):

```sh
# Determinate Systems installer — turns on flakes by default, easiest path
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
```

Alternative: the [official installer](https://nixos.org/download) — then enable flakes by adding `experimental-features = nix-command flakes` to `~/.config/nix/nix.conf`.

**2. Clone the repo**:

```sh
git clone https://github.com/ankorio/twinflame
cd apkdiff
```

That's it — there's nothing else to install.

## Running

### One-shot CLI via `nix run`

`nix run` builds the tool on demand and invokes it. The first run compiles Redex from source (~5 min); after that everything is cached.

```sh
# diff two APKs, scope to a vendor package, write a JSON report
nix run . -- old.apk new.apk \
    --auto-package \
    --threshold 0.8 \
    --json report.json

# real-world example: vuln/patched pair, Redex-normalized to defeat
# junk-instruction obfuscation, with the obfuscated-package fallback
# so Proguard-renamed packages cluster together for comparison
nix run . -- vuln.apk patched.apk --normalize --find-obfuscated
```

Output format (one line per non-perfect match):

```
[+] com.acme.foo: Login - Login.java | com.acme.foo: Login - SourceFile -> 0.8523
      ~ validateToken (0.6100)                              # method modified
      - legacyHash                                          # method removed
      + verifyNonce                                         # method added
[-] com.acme.foo: DeletedClass - DeletedClass.java          # in lhs only
[*] com.acme.foo: AddedClass - SourceFile                   # in rhs only
```

Each `[+]` paired line is followed by the methods that actually changed, so you
see *which* method moved the class score, not just that the class changed. The
JSON report (`--json`) carries the full per-method verdict list for every match.

Timings go to stderr; the diff itself to stdout (so you can `| less` or redirect freely).

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
nix build              # produces ./result/bin/apkdiff
./result/bin/apkdiff --help
```

### Dev shell

For interactive use (REPL, hacking on the code, running tests):

```sh
nix develop            # enter shell with python + redex + jadx on PATH
pytest tests/          # 86 tests, ~8s
python -m apkdiff.cli --help
```

### Python API

```python
from apkdiff import load, filter, diff

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

`--deobfuscation-map OUT` writes a ProGuard-format `mapping.txt` for **apk2** (the obfuscated/stripped build) by propagating class names recovered from the matched classes in **apk1** (the donor build that still carries the DEX `SourceFile` attribute). Load it into JADX/Ghidra and the rename propagates to every reference automatically — apkdiff never rewrites the DEX.

```sh
# apk1 = older build that kept SourceFile; apk2 = the one you want to read
apkdiff app-old.apk app-new.apk \
    --find-obfuscated --deobfuscation-map app-new.map --map-min-confidence 0.8
```

- Recovers the class **simple name** (`ContextCompat.java` → `x6.q` becomes `x6.ContextCompat`); the obfuscated **package** and inner-class structure are kept (source files carry no package).
- Anchored / exact (`distance == 1.0`) matches are trusted; lower-confidence ones are still emitted but flagged with a `# low-confidence` comment. Tune the floor with `--map-min-confidence`.
- This is the cross-version superpower over single-APK source-file deobfuscation: it names classes in a build that **stripped** `SourceFile`, using the build that didn't. Method/field name propagation is a planned next step.

### Workflow recipes

- **Patch identification.** Scope with `--package PREFIX` or `--auto-package` (drastically shrinks the comparison set), use `--threshold 0.8` since bug fixes are minor changes. Skip `distance == 1.0` matches.
- **Modded / repackaged app.** Drop `--package` (mods often live in bundled SDKs, not the vendor package). If you see a sea of ~0.95 "everything changed" matches, run with `--normalize` — that's the signature of junk-instruction obfuscation.
- **Heavily Proguard'd inputs.** Add `--find-obfuscated` so single-letter package names (`a.b.c`) collapse into a fallback pool rather than failing to pair with their clear-text counterparts.
- **Deobfuscating a stripped build.** Diff it against an older build that kept `SourceFile`, add `--find-obfuscated --deobfuscation-map out.map`, then load `out.map` in JADX.

## Layout

| Module                                               | Role                                                                 |
| ---------------------------------------------------- | -------------------------------------------------------------------- |
| [src/apkdiff/model.py](src/apkdiff/model.py)         | Dataclasses: `App`, `Class`, `Method`, `Field`, `Signature`, `Match`, `MethodMatch` |
| [src/apkdiff/loader.py](src/apkdiff/loader.py)       | androguard wrapping + multi-DEX merge; captures calls/strings/xrefs  |
| [src/apkdiff/manifest.py](src/apkdiff/manifest.py)   | `AndroidManifest.xml` → `ManifestInfo`; dev-package suggestion       |
| [src/apkdiff/normalize.py](src/apkdiff/normalize.py) | Redex subprocess wrapper                                             |
| [src/apkdiff/cluster.py](src/apkdiff/cluster.py)     | Package-based pools + entropy/length obfuscation fallback            |
| [src/apkdiff/anchor.py](src/apkdiff/anchor.py)       | Stage B: string-IDF + framework-call seed matches                    |
| [src/apkdiff/signature.py](src/apkdiff/signature.py) | 128-bit SimHash + LSH bucket index                                   |
| [src/apkdiff/accurate.py](src/apkdiff/accurate.py)   | Abstract opcodes, method-level + class scoring, greedy 1-to-1 assignment |
| [src/apkdiff/opcodes.py](src/apkdiff/opcodes.py)     | Dalvik opcode → 13-category lookup table                             |
| [src/apkdiff/\_hot.py](src/apkdiff/_hot.py)          | Hot loops (popcount, Hamming, Levenshtein) — Rust seam               |
| [src/apkdiff/deobf.py](src/apkdiff/deobf.py)         | Cross-version deobfuscation: recovered names → ProGuard mapping.txt   |
| [src/apkdiff/api.py](src/apkdiff/api.py)             | Public `load / filter / diff` entry points                           |
| [src/apkdiff/cli.py](src/apkdiff/cli.py)             | `apkdiff` console script                                             |

## Tests

```sh
nix develop --command pytest tests/
```

Currently: **86 tests passing**, 0 skipped. Redex is built and on PATH inside the dev shell.

Test fixtures are **pure-synthetic**: `tests/fixtures/synthetic.py` builds `Class` objects directly via Python, no binaries committed to git. The integration tests exercise the full diff pipeline (cluster → anchor → signature → accurate → api) on these objects.

The on-disk DEX/APK round-trip (loader.py end-to-end) currently has a smoke-only test; a real binary DEX emitter under [tests/fixtures/build_dex.py](tests/fixtures/build_dex.py) is stubbed for future work.

## Known limitations

- **Identical-structure classes collide.** Pools where many classes share signatures (trivial getter/setter classes, generated stubs) may produce false pairings via greedy assignment. `--min-instr 5` filters trivial classes; raising `--neighbors` widens the candidate pool.
- **No inheritance context.** Matching is per-class (now with method-level detail *inside* a paired class), but ignores first-level parent/child signals (cf. LibPecker); these can be added later without disturbing the core.
- **No cross-boundary / optimization resilience yet.** Matching assumes a 1:1 class and method correspondence. R8 *optimizations* that break that assumption — inlining, outlining, class merging — are a planned future track (see `apkdiff-next-dev-plan.md`, M3.1), not yet implemented.
- **JNI / native-code changes are invisible.** The diff is purely Dalvik-level — native library mutations require a complementary native-code diff.
- **LSH is approximate by design.** `--buckets N` tunes the accuracy/speed knob.

## License

MIT.
