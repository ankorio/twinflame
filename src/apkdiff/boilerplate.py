"""Generated-boilerplate detection (matcher-side, obfuscation-robust).

Some generated classes are *structural twins* — many near-identical copies that
carry no reviewable logic (Kotlin `sortedBy`/`compareBy` comparators, ViewBinding
holders, `R` resource classes). They hurt twice: they collide in the matcher
(one twin mispairs to another) and they flood the change report with noise a
reviewer doesn't care about. Skipping them lifts precision on the classes that
*do* matter and cleans the report.

Detection must survive R8, so it can't rely on class names (renamed) or
`SourceFile` (R8 rewrites it to the literal `"SourceFile"`). Measured on a real
release build (Fossify Contacts, see eval/corpus/README.md):

- **Comparator twins** — `java/util/Comparator` is a framework interface, kept
  verbatim; the generated ones are tiny (just `compare`, maybe a bridge). Robust.
- **ViewBinding / R** — *not* structurally detectable in a fully-obfuscated build
  (R8 strips the `androidx.viewbinding.ViewBinding` interface and inlines/strips
  `R`), so they're handled where names survive: the eval oracle (original names
  from mapping.txt) and, for `R`, the existing 0-instruction filter. Documented
  as a known limitation for obfuscated inputs without those signals.
"""

from __future__ import annotations

from .model import Class

COMPARATOR = "Ljava/util/Comparator;"
# A generated comparator is just its compare method (+ an optional synthetic
# bridge). A hand-written comparator with real logic has more, so this floor
# keeps those while catching the twins.
_COMPARATOR_MAX_METHODS = 2


def is_boilerplate(c: Class) -> bool:
    """Whether `c` is a generated structural twin worth skipping (obfuscation-
    robust signals only)."""
    if COMPARATOR in c.interfaces and len(c.methods) <= _COMPARATOR_MAX_METHODS:
        return True
    return False
