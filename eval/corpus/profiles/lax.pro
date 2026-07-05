# Profile: lax — identifier renaming + shrinking only, NO optimization.
# Turns the app's -optimize config into a rename-only build: classes/methods
# are renamed and dead code stripped, but nothing is inlined, merged, or
# repackaged. This is the "easy" tier the anchor/rename pipeline is built for.
-dontoptimize
