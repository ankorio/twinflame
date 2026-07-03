#!/usr/bin/env bash
# Build one app version under several obfuscation profiles.
#
# A profile is applied by appending eval/corpus/profiles/<name>.pro to the app's
# app/proguard-rules.pro (and, if present, eval/corpus/profiles/<name>.gradle.properties
# to gradle.properties) before a clean release build, then reverting. This needs
# no edits to the app's own gradle logic and works for any app that references
# `proguard-rules.pro` (Clock, Contacts, OpenCamera all do). The special profile
# name `optimize` appends nothing (the app's shipped -optimize config as-is).
#
# Usage:
#   build_profiles.sh <repo_dir> <assemble_task> <apk_glob> <mapping_rel> <out_root> <profile...>
# Example (Clock 2.31):
#   git -C corpus/Clock checkout -f 2.31
#   eval/corpus/build_profiles.sh corpus/Clock :app:assembleRelease \
#       'app/build/outputs/apk/release/*.apk' \
#       app/build/outputs/mapping/release/mapping.txt \
#       corpus/profiles-out/clock-2.31 lax optimize strict
set -euo pipefail

REPO=$1; TASK=$2; APKGLOB=$3; MAPREL=$4; OUTROOT=$5; shift 5
PROFILES_DIR="$(cd "$(dirname "$0")/profiles" && pwd)"
export JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}
export ANDROID_HOME=${ANDROID_HOME:-$HOME/Android/Sdk}

cd "$REPO"
RULES=app/proguard-rules.pro
cp "$RULES" "$RULES.profbak"
[ -f gradle.properties ] && cp gradle.properties gradle.properties.profbak || true
echo "sdk.dir=$ANDROID_HOME" > local.properties
chmod +x gradlew

restore() {
  cp "$RULES.profbak" "$RULES"; rm -f "$RULES.profbak"
  [ -f gradle.properties.profbak ] && mv gradle.properties.profbak gradle.properties || true
}
trap restore EXIT

for P in "$@"; do
  cp "$RULES.profbak" "$RULES"
  [ -f gradle.properties.profbak ] && cp gradle.properties.profbak gradle.properties || true
  [ -f "$PROFILES_DIR/$P.pro" ] && cat "$PROFILES_DIR/$P.pro" >> "$RULES"
  [ -f "$PROFILES_DIR/$P.gradle.properties" ] && cat "$PROFILES_DIR/$P.gradle.properties" >> gradle.properties
  echo "===== profile: $P ====="
  sh ./gradlew --no-daemon clean $TASK 2>&1 | tail -2
  OUT="$OUTROOT/$P"; mkdir -p "$OUT"
  cp $APKGLOB "$OUT/app.apk"
  cp "$MAPREL" "$OUT/mapping.txt"
  echo "  saved -> $OUT"
done
echo "PROFILES DONE: $*"
