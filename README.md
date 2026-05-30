# apkdiff

DEX-level Android APK class-diffing engine. Given two APK versions, emits a ranked list of class matches with a normalized similarity score so you can isolate exactly which classes mutated between releases. Implements the four-stage architecture from Quarkslab's *"Android Application Diffing: Engine Overview"* (Czayka & Thomas, 2019).

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

## Quickstart

```sh
nix develop          # enter the pinned dev shell
nix build            # produce ./result/bin/apkdiff
nix run . -- --help  # invoke the CLI directly
```

Python API:

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

CLI:

```sh
apkdiff old.apk new.apk \
    --auto-package \
    --threshold 0.8 \
    --neighbors 3 \
    --min-instr 5 \
    --json report.json
```

Workflows:
- **Patch identification**: scope with `--package` or `--auto-package` (drastically shrinks the comparison set), use `--threshold 0.8` since bug fixes are minor changes. Skip `distance == 1.0` matches.
- **Modded/repackaged app**: drop `--package` (mods often live in bundled SDKs, not the vendor package). If you see a sea of ~0.95 "everything changed" matches, run with `--normalize` — that's the signature of junk-instruction obfuscation.

## Layout

| Module                                              | Role                                                                 |
|-----------------------------------------------------|----------------------------------------------------------------------|
| [src/apkdiff/model.py](src/apkdiff/model.py)        | Dataclasses: `App`, `Class`, `Method`, `Field`, `Signature`, `Match` |
| [src/apkdiff/loader.py](src/apkdiff/loader.py)      | androguard wrapping + multi-DEX merge                                |
| [src/apkdiff/manifest.py](src/apkdiff/manifest.py)  | `AndroidManifest.xml` → `ManifestInfo`; dev-package suggestion       |
| [src/apkdiff/normalize.py](src/apkdiff/normalize.py)| Redex subprocess wrapper                                             |
| [src/apkdiff/cluster.py](src/apkdiff/cluster.py)    | Package-based pools + entropy/length obfuscation fallback            |
| [src/apkdiff/signature.py](src/apkdiff/signature.py)| 128-bit SimHash + LSH bucket index                                   |
| [src/apkdiff/accurate.py](src/apkdiff/accurate.py)  | Abstract opcodes, Levenshtein scoring, greedy 1-to-1 assignment      |
| [src/apkdiff/opcodes.py](src/apkdiff/opcodes.py)    | Dalvik opcode → 13-category lookup table                             |
| [src/apkdiff/_hot.py](src/apkdiff/_hot.py)          | Hot loops (popcount, Hamming, Levenshtein) — Rust seam               |
| [src/apkdiff/api.py](src/apkdiff/api.py)            | Public `load / filter / diff` entry points                           |
| [src/apkdiff/cli.py](src/apkdiff/cli.py)            | `apkdiff` console script                                             |

## Tests

```sh
nix develop --command pytest tests/
```

Currently: 62 tests passing, 1 skipped (Redex normalization smoke — runs only when `redex` is on PATH).

Test fixtures are **pure-synthetic**: `tests/fixtures/synthetic.py` builds `Class` objects directly via Python, no binaries committed to git. The integration tests exercise the full diff pipeline (cluster → signature → accurate → api) on these objects.

The on-disk DEX/APK round-trip (loader.py end-to-end) currently has a smoke-only test; a real binary DEX emitter under [tests/fixtures/build_dex.py](tests/fixtures/build_dex.py) is stubbed for future work.

## Known limitations

- **Identical-structure classes collide.** Pools where many classes share signatures (trivial getter/setter classes, generated stubs) may produce false pairings via greedy assignment. `--min-instr 5` filters trivial classes; raising `--neighbors` widens the candidate pool.
- **No inheritance context (v1).** Per-class comparison only. First-level parent/child signals (cf. LibPecker) can be added later without disturbing the core.
- **JNI / native-code changes are invisible.** The diff is purely Dalvik-level — native library mutations require a complementary native-code diff.
- **LSH is approximate by design.** `--buckets N` tunes the accuracy/speed knob.

## License

MIT.
