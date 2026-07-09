# Obfuscation profiles

Documentation moved to the wiki: **[Obfuscation
Profiles](https://github.com/ankorio/twinflame/wiki/Obfuscation-Profiles)** — the R8 strength
axis (lax / optimize / strict), the engine axis (R8 full/compat, ProGuard slot), and how
profile builds are graded.

This directory keeps the rule snippets themselves (`lax.pro`, `strict.pro`; `optimize` is the
app's shipped config). They are applied by [`../build_profiles.sh`](../build_profiles.sh),
which appends `<name>.pro` to the app's `app/proguard-rules.pro` for a clean release build.
