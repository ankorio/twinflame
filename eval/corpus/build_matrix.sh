#!/usr/bin/env bash
# Build the full OSS calibration matrix: 3 projects x 2 versions x 3 R8 profiles
# into a persistent tree so benchmarks never rebuild.
#
#   corpus/oss_matrix/<project>/<version>/<profile>/{app.apk,mapping.txt}
#
# Reuses eval/corpus/build_profiles.sh (applies lax/optimize/strict by appending
# to app/proguard-rules.pro). We checkout each tag here, then delegate.
set -uo pipefail

CORPUS=/mnt/vault/Reversing/apkdiff/corpus
SRC=$CORPUS/OSS_projects_src
BP=/mnt/vault/Reversing/apkdiff/twinflame/eval/corpus/build_profiles.sh
OUT=$CORPUS/oss_matrix
export JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}
export ANDROID_HOME=${ANDROID_HOME:-$HOME/Android/Sdk}
PROFILES="lax optimize strict"

# project  repo          task                     apkglob                                     maprel                                               tagA tagB
ROWS=(
  "clock    Clock         :app:assembleRelease     app/build/outputs/apk/release/*.apk         app/build/outputs/mapping/release/mapping.txt        2.30   2.31"
  "contacts Contacts      :app:assembleFossRelease app/build/outputs/apk/foss/release/*.apk    app/build/outputs/mapping/fossRelease/mapping.txt    1.5.0  1.6.0"
  "calc     CalculatorM3  :app:assembleRelease     app/build/outputs/apk/release/*.apk         app/build/outputs/mapping/release/mapping.txt        v1.4.3 v1.5.2"
)

overall=0
for row in "${ROWS[@]}"; do
  read -r proj repo task apkglob maprel tagA tagB <<<"$row"
  for tag in "$tagA" "$tagB"; do
    echo "############## $proj @ $tag ##############"
    ( cd "$SRC/$repo" && git checkout -f "$tag" 2>&1 | tail -1 ) || { echo "!! checkout $repo $tag failed"; overall=1; continue; }
    if bash "$BP" "$SRC/$repo" "$task" "$apkglob" "$maprel" "$OUT/$proj/$tag" $PROFILES; then
      echo ">>> OK $proj/$tag"
    else
      echo "!! BUILD FAILED $proj/$tag"; overall=1
    fi
  done
done

echo "================= MATRIX SUMMARY ================="
find "$OUT" -name app.apk 2>/dev/null | sort | while read -r f; do
  d=$(dirname "$f"); sz=$(du -h "$f" | cut -f1)
  m=$([ -f "$d/mapping.txt" ] && echo "map:ok" || echo "map:MISSING")
  echo "  ${d#$OUT/}  ($sz, $m)"
done
echo "expected 18 rows; overall_exit=$overall"
exit $overall
