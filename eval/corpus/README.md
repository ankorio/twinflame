# Evaluation corpora

Documentation for the evaluation corpora and the accuracy results moved to the wiki:

- **[Evaluation Corpus](https://github.com/ankorio/twinflame/wiki/Evaluation-Corpus)** — what each
  corpus grades, the persistent OSS build matrix, supplied-binary apps, and the build recipes.
- **[Accuracy Benchmarks](https://github.com/ankorio/twinflame/wiki/Accuracy-Benchmarks)** — the
  P/R/F1 scoreboard, diagnostics, and the measurement history.
- **[Obfuscation Profiles](https://github.com/ankorio/twinflame/wiki/Obfuscation-Profiles)** — the
  R8 strength axis implemented by `profiles/`.

This directory keeps the reproducible tooling: `build_matrix.sh` (full OSS matrix),
`build_profiles.sh` (one version × obfuscation profiles), `build_xversion.sh` (two release
tags), and the `profiles/*.pro` rule snippets. **No APKs or DEX are committed** — build outputs
go to a `corpus/` working area alongside the repo checkout, outside git.
