# apkdiff

DEX-level Android APK class-diffing engine. Given two APK versions, emits a ranked list of class matches with a normalized similarity score so you can isolate exactly which classes mutated between releases. Implements the four-stage architecture from Quarkslab's _"Android Application Diffing: Engine Overview"_ (Czayka & Thomas, 2019).

Use cases:

- **Vulnerability patch identification** — find the class a vendor changed to fix a flaw.
- **Repackaging / mod analysis** — find injected code that replaces vendor logic with no-ops or hostile callbacks.
- **Deobfuscation correlation** — pair classes across Proguard-renamed builds.

## Pipeline

1. **Multi-DEX merge** — every `classes*.dex` is unioned into one logical view (first-wins on descriptor, matching ART).
2. **Stage 1 — clustering.** Classes group into pools keyed by package name; matching pools across the two APKs compare in parallel. Obfuscated packages (Shannon entropy + length heuristic) fall into a single fallback pool.
3. **Stage 2 — bulk SimHash + bucketed LSH.** Each class gets a 128-bit signature built from four 32-bit partial hashes — `cls`, `fld`, `mth`, `code` — over **structure-only** features (no names, no strings). Nearest neighbors are found via Dullien-style bit-permutation LSH instead of all-pairs.
4. **Stage 3 — accurate comparison.** Top-k Stage-2 neighbors are re-scored with per-feature similarities plus Levenshtein distance over per-class abstract-opcode sequences (Dalvik opcodes mapped to 13 semantic categories; method order normalized via 16-bit method signature). Greedy 1-to-1 assignment resolves ties.

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
[-] com.acme.foo: DeletedClass - DeletedClass.java          # in lhs only
[*] com.acme.foo: AddedClass - SourceFile                   # in rhs only
```

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
pytest tests/          # 63 tests, ~8s
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

### Workflow recipes

- **Patch identification.** Scope with `--package PREFIX` or `--auto-package` (drastically shrinks the comparison set), use `--threshold 0.8` since bug fixes are minor changes. Skip `distance == 1.0` matches.
- **Modded / repackaged app.** Drop `--package` (mods often live in bundled SDKs, not the vendor package). If you see a sea of ~0.95 "everything changed" matches, run with `--normalize` — that's the signature of junk-instruction obfuscation.
- **Heavily Proguard'd inputs.** Add `--find-obfuscated` so single-letter package names (`a.b.c`) collapse into a fallback pool rather than failing to pair with their clear-text counterparts.

## Layout

| Module                                               | Role                                                                 |
| ---------------------------------------------------- | -------------------------------------------------------------------- |
| [src/apkdiff/model.py](src/apkdiff/model.py)         | Dataclasses: `App`, `Class`, `Method`, `Field`, `Signature`, `Match` |
| [src/apkdiff/loader.py](src/apkdiff/loader.py)       | androguard wrapping + multi-DEX merge                                |
| [src/apkdiff/manifest.py](src/apkdiff/manifest.py)   | `AndroidManifest.xml` → `ManifestInfo`; dev-package suggestion       |
| [src/apkdiff/normalize.py](src/apkdiff/normalize.py) | Redex subprocess wrapper                                             |
| [src/apkdiff/cluster.py](src/apkdiff/cluster.py)     | Package-based pools + entropy/length obfuscation fallback            |
| [src/apkdiff/signature.py](src/apkdiff/signature.py) | 128-bit SimHash + LSH bucket index                                   |
| [src/apkdiff/accurate.py](src/apkdiff/accurate.py)   | Abstract opcodes, Levenshtein scoring, greedy 1-to-1 assignment      |
| [src/apkdiff/opcodes.py](src/apkdiff/opcodes.py)     | Dalvik opcode → 13-category lookup table                             |
| [src/apkdiff/\_hot.py](src/apkdiff/_hot.py)          | Hot loops (popcount, Hamming, Levenshtein) — Rust seam               |
| [src/apkdiff/api.py](src/apkdiff/api.py)             | Public `load / filter / diff` entry points                           |
| [src/apkdiff/cli.py](src/apkdiff/cli.py)             | `apkdiff` console script                                             |

## Tests

```sh
nix develop --command pytest tests/
```

Currently: **63 tests passing**, 0 skipped. Redex is built and on PATH inside the dev shell.

Test fixtures are **pure-synthetic**: `tests/fixtures/synthetic.py` builds `Class` objects directly via Python, no binaries committed to git. The integration tests exercise the full diff pipeline (cluster → signature → accurate → api) on these objects.

The on-disk DEX/APK round-trip (loader.py end-to-end) currently has a smoke-only test; a real binary DEX emitter under [tests/fixtures/build_dex.py](tests/fixtures/build_dex.py) is stubbed for future work.

## Known limitations

- **Identical-structure classes collide.** Pools where many classes share signatures (trivial getter/setter classes, generated stubs) may produce false pairings via greedy assignment. `--min-instr 5` filters trivial classes; raising `--neighbors` widens the candidate pool.
- **No inheritance context (v1).** Per-class comparison only. First-level parent/child signals (cf. LibPecker) can be added later without disturbing the core.
- **JNI / native-code changes are invisible.** The diff is purely Dalvik-level — native library mutations require a complementary native-code diff.
- **LSH is approximate by design.** `--buckets N` tunes the accuracy/speed knob.

## License

MIT.
