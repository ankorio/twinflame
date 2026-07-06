"""Class provenance — app vs. library discrimination (change-detection Phase 2.1).

The change report on a real app is dominated by **bundled-library churn**: an
app ships hundreds of third-party classes (Play Services, OkHttp, Jackson,
WorkManager, Kotlin stdlib) that move between builds for reasons that have
nothing to do with the developer's own edits. For triage we want the
developer's **own** code surfaced first. This module labels each class:

- ``"app"``     — the developer's own code (highest review priority).
- ``"library"`` — a bundled third party (demoted; usually version churn).
- ``"unknown"`` — can't tell (ranked between the two).

Signals, in priority order (first match wins):

1. **Under the dev package prefix** -> ``app``. The strongest, most general
   signal; supplied via ``--package`` / ``--auto-package`` (manifest). Survives
   R8 rename for apps that don't repackage their own code (e.g. an app that keeps
   ``com/acme/app``). If a build repackages app classes to the root
   (``-repackageclasses ''``) this signal is lost and such classes fall to
   ``unknown`` — a documented limitation, not a wrong answer.
2. **Maven-coordinate SourceFile** -> ``library``. androguard reports the
   artifact coordinate (``group:artifact@@version``) as the ``SourceFile`` for
   AAR-bundled classes; a real source filename can never contain ``:``. Survives
   obfuscation (the string is retained) even when the *package* was renamed.
3. **Known library descriptor prefix** -> ``library``. Catches un-obfuscated
   third parties whose package the build left intact.

No network, no I/O — pure function of descriptor + source file + dev prefix, so
it stays inside the "high-performance CLI" budget.
"""

from __future__ import annotations

from typing import Iterable, Optional, Union

from .model import Class

# Descriptor prefixes owned by the framework or common third parties. Checked
# only after the dev-package test, so an app deliberately namespaced under one
# of these (rare) is still classified ``app`` when the dev prefix is known.
LIBRARY_PREFIXES: tuple[str, ...] = (
    "Landroid/",
    "Landroidx/",
    "Ljava/",
    "Ljavax/",
    "Lkotlin/",
    "Lkotlinx/",
    "Lcom/google/",
    "Lcom/fasterxml/",
    "Lavro/",
    "Lokhttp3/",
    "Lokio/",
    "Lretrofit2/",
    "Lcom/squareup/",
    "Lio/grpc/",
    "Lio/reactivex/",
    "Lio/sentry/",
    "Ldagger/",
    "Lorg/",
    "Lcom/bumptech/",
    "Lcom/facebook/",
    "Lcom/jakewharton/",
)

# Review-priority ordering used by the change ranker (lower = surfaced first).
ORIGIN_RANK = {"app": 0, "unknown": 1, "library": 2}


def dev_descriptor_prefix(package: Optional[str]) -> Optional[str]:
    """Normalise a dev package (``com.acme.app``, ``com/acme/app``,
    or ``Lcom/acme/app;``) into a descriptor prefix ``Lcom/acme/app/``
    suitable for ``str.startswith``. ``None``/empty -> ``None``."""
    if not package:
        return None
    p = package.strip()
    if p.startswith("L") and p.endswith(";"):  # descriptor form Les/foo/Bar;
        p = p[1:-1]
    p = p.replace(".", "/").strip("/")
    if not p:
        return None
    return "L" + p + "/"


# A dev-prefix argument may be a single prefix, a tuple of them (multi-root
# apps), or None. `str.startswith` accepts a str or a tuple of str directly.
DevPrefix = Union[str, tuple[str, ...], None]


def dev_descriptor_prefixes(packages: Union[str, Iterable[str], None]) -> tuple[str, ...]:
    """Normalise one or many dev packages into descriptor prefixes (multi-root
    apps commonly ship under several, e.g. `com.acme.app.*` and `com.acme.other.*`)."""
    if packages is None:
        return ()
    if isinstance(packages, str):
        packages = [packages]
    out: list[str] = []
    for p in packages:
        d = dev_descriptor_prefix(p)
        if d and d not in out:
            out.append(d)
    return tuple(out)


def classify_origin(
    descriptor: str, source_file: Optional[str], dev_prefix: DevPrefix
) -> str:
    """Label one class. ``dev_prefix`` is a descriptor prefix, a tuple of them
    (multi-root apps), or ``None``/empty when the dev package is unknown."""
    if dev_prefix and descriptor.startswith(dev_prefix):
        return "app"
    if source_file and ":" in source_file:
        return "library"
    if descriptor.startswith(LIBRARY_PREFIXES):
        return "library"
    return "unknown"


def origin_of(c: Class, dev_prefix: DevPrefix) -> str:
    return classify_origin(c.descriptor, c.source_file, dev_prefix)
