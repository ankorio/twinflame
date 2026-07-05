# `--changes-json` schema (v1)

The stable, machine-consumable output of `apkdiff … --changes-json OUT`. Downstream
tooling (decompiler alignment, diff viewers) should consume **this**, not the human
`--changes` text. The shape is versioned by the top-level `schema_version`
(`apkdiff.changes.CHANGES_SCHEMA_VERSION`); a breaking change bumps it.

## Top level

```json
{
  "schema_version": 1,
  "summary": { "modified": 384, "added": 282, "removed": 135, "cosmetic": 414, "unchanged": 10436 },
  "changes": [ /* one object per class, ranked most-review-worthy first */ ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Format version. Consumers should check this. |
| `summary` | object | Count per `kind` (keys absent when zero). |
| `changes` | array | One entry per class, pre-sorted (see **Ordering**). |

## A `changes[]` entry

```json
{
  "kind": "modified",
  "lhs": "Lorg/telegram/ui/ArticleViewer;",
  "rhs": "Lorg/telegram/ui/ArticleViewer;",
  "source_file": "SourceFile",
  "origin": "app",
  "confidence": "high",
  "magnitude": 95,
  "match_distance": 0.87,
  "anchored": true,
  "delta": { /* only for kind=modified; see below */ },
  "methods": [ /* only for kind=modified when localizable; see below */ ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `kind` | enum | `modified` \| `added` \| `removed` \| `cosmetic` \| `unchanged` (see **Kinds**). |
| `lhs` | string \| null | Old-build class descriptor (`L…;`). `null` when `added`. |
| `rhs` | string \| null | New-build class descriptor. `null` when `removed`. |
| `source_file` | string \| null | `SourceFile` attribute if retained (R8 often rewrites it to the literal `"SourceFile"` or strips it). |
| `origin` | enum | `app` \| `library` \| `unknown` — provenance (see **Origin**). |
| `confidence` | enum | `high` \| `low` (see **Confidence**). Non-`modified` is always `high`. |
| `magnitude` | int | Review-worthiness. For `modified`: feature-delta count + methods added/deleted. For `added`/`removed`: class size in instructions. Else `0`. |
| `match_distance` | float | Structural similarity of the underlying match in `[0,1]` (`1.0` = identical). `0.0` for `added`/`removed`. |
| `anchored` | bool | Match rests on a rename-invariant string / framework-call anchor. |
| `delta` | object \| absent | Present only for `modified`. |
| `methods` | array \| absent | Per-method localization; present for `modified` when method-level changes are localizable. |

### `delta` (modified only)

All arrays are sorted; framework calls/strings/types are full descriptors. `app_types_*`
are in **new-build** naming (translated through the class-match map).

```json
"delta": {
  "calls_added":    ["Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;"],
  "calls_removed":  [],
  "strings_added":  ["v2/pay"],
  "strings_removed":["v1/pay"],
  "types_added":    ["Landroidx/appcompat/app/AppCompatActivity;"],
  "types_removed":  [],
  "app_types_added":   ["Lorg/telegram/ui/Foo;"],
  "app_types_removed": []
}
```

`calls_*` = framework/library call targets; `strings_*` = string constants;
`types_*` = framework type references (super/interface/field/param); `app_types_*` =
*app* type dependencies (only populated when a class-match map was available).

### `methods[]` (modified only)

```json
"methods": [
  { "status": "modified", "name": "onCreate", "descriptor": "(Landroid/os/Bundle;)V",
    "instr_count": 214, "calls_added": ["…Uri;->parse…"], "calls_removed": ["…Log;->d…"] },
  { "status": "added",   "name": "helper", "descriptor": "()V", "instr_count": 12,
    "calls_added": ["…"], "calls_removed": [] }
]
```

`status` ∈ `modified` \| `added` \| `deleted`. `name`/`descriptor` are obfuscated when the
build renames them — resolve via the `--deobfuscation-map`. `modified` methods appear only
when their **framework-call set** changed (a body-only re-optimization isn't localized).

## Kinds

| Kind | Meaning | `delta`/`methods` |
|---|---|---|
| `modified` | Matched; semantic features or method structure changed — a real edit. | yes |
| `added` | Only in the new build. | no |
| `removed` | Only in the old build. | no |
| `cosmetic` | Matched, structurally different, but **no** semantic-feature change → re-obfuscation/re-optimization noise. Emitted, demoted. | no |
| `unchanged` | Matched and structurally identical. | no |

## Origin

- `app` — under the developer package (`--app-package`/`--package`, else the manifest). Highest priority.
- `library` — bundled third party (known prefix or a maven-coordinate `SourceFile`). Demoted.
- `unknown` — can't tell (e.g. R8 repackaged it to a short name). Ranked between.

## Confidence

- `high` — trustworthy change.
- `low` — a `modified` resting **only** on framework-call churn in **non-app** code. Different
  R8 versions relocate calls between classes via inlining, so this is the cross-toolchain-noise
  signature (measured 95% of false-`modified` on a same-source cross-R8 rebuild). Filter these
  out with `--min-confidence high`.

## Ordering

`changes[]` is pre-sorted most-review-worthy first: **kind** (`modified` → `added` → `removed`
→ `cosmetic` → `unchanged`), then **origin** (`app` → `unknown` → `library`), then **confidence**
(`high` → `low`), then descending `magnitude`. Consumers may re-sort; the `origin`/`confidence`/
`magnitude` fields carry everything needed to filter or re-rank.

## Design contract

"Demote, never drop": by default every matched/added/removed class is emitted with a score and
category — the report is complete and the *consumer* thresholds. `--min-confidence high` is the
one built-in filter (drops `low`-confidence `modified`).
