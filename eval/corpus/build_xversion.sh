#!/bin/sh
# Build two release tags of a Gradle Android app and stage them for the
# cross-version matching scorer (eval.xversion). The scorer's oracle is the
# join of the two builds' own R8 mapping.txt on original class name, so no
# connecting git history is needed — a shallow clone with both tags is enough.
#
# Usage:
#   build_xversion.sh <repo_dir> <tagA> <tagB> <gradle_task> <out_dir> [gradle_ver]
#
# Example (Fossify Contacts, ~1-year major span):
#   build_xversion.sh ~/corpus/Contacts 1.0.0 1.6.0 assembleFossRelease \
#       ~/corpus/xversion/contacts_major 8.13
#
# Then grade:
#   python -m eval.xversion \
#     <out>/1.0.0/app.apk <out>/1.0.0/mapping.txt \
#     <out>/1.6.0/app.apk <out>/1.6.0/mapping.txt --package org.fossify.contacts
set -eu

REPO=$1; TAG_A=$2; TAG_B=$3; TASK=$4; OUT=$5; GRADLE_VER=${6:-}
: "${JAVA_HOME:=/usr/lib/jvm/java-21-openjdk-amd64}"
export JAVA_HOME

build_one() {
  tag=$1
  echo ">>> building $tag ($TASK)"
  cd "$REPO"
  git checkout -f "$tag"
  rm -rf app/build
  # Optionally pin the Gradle wrapper (older tags may declare a Gradle that the
  # installed JDK doesn't support; a newer wrapper usually still runs old AGP).
  if [ -n "$GRADLE_VER" ]; then
    sed -i "s|gradle-[0-9.]*-bin.zip|gradle-${GRADLE_VER}-bin.zip|" \
      gradle/wrapper/gradle-wrapper.properties
  fi
  chmod +x gradlew
  ./gradlew "$TASK" --no-daemon

  dest="$OUT/$tag"; mkdir -p "$dest"
  apk=$(find app/build/outputs/apk -name '*release*.apk' | head -1)
  map=$(find app/build/outputs/mapping -name 'mapping.txt' | head -1)
  [ -n "$apk" ] || { echo "!! no release APK found for $tag"; exit 1; }
  [ -n "$map" ] || { echo "!! no mapping.txt found for $tag (R8 off?)"; exit 1; }
  cp "$apk" "$dest/app.apk"
  cp "$map" "$dest/mapping.txt"
  echo "    staged -> $dest/{app.apk,mapping.txt}"
}

build_one "$TAG_A"
build_one "$TAG_B"
echo "done. grade with: python -m eval.xversion \\"
echo "  $OUT/$TAG_A/app.apk $OUT/$TAG_A/mapping.txt \\"
echo "  $OUT/$TAG_B/app.apk $OUT/$TAG_B/mapping.txt --package <app.package>"
