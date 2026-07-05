# Profile: strict — maximal R8 aggression on top of the app's -optimize config.
# -allowaccessmodification lets R8 widen access so it can inline/merge across
# visibility boundaries; -repackageclasses '' flattens every class into the root
# package (destroys package structure our clustering would otherwise use). This
# is the closest a free R8 config gets to a commercial obfuscator, and the
# hardest tier for structure-based matching (aggressive inlining + merging).
#
# NOTE: R8 honours -allowaccessmodification and -repackageclasses. The classic
# ProGuard optimization knobs below are IGNORED by R8 (it has its own optimizer)
# but take effect under the `proguard` obfuscator profile — kept here so the one
# snippet describes "strict" for both engines.
-allowaccessmodification
-repackageclasses ''
-overloadaggressively
-optimizationpasses 5
-mergeinterfacesaggressively
