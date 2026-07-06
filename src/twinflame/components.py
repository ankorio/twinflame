"""Sensitive-component detection — malware-review aid (feedback point #4).

Android gates the most abuse-prone capabilities behind a handful of framework
base classes/interfaces: a class *is* a notification-reader, an accessibility
service, or a device-admin receiver by virtue of extending the right
`android.*` type. R8/obfuscation renames the app's own identifiers but **cannot**
rename framework superclasses/interfaces — `Landroid/...;` descriptors survive
verbatim — so the type-graph edges already captured on `Class` (superclass /
interfaces) are a reliable, obfuscation-proof signal.

We resolve each class's superclass chain (transitively, through the app's own
classes) plus its declared interfaces, and label it with any sensitive base it
reaches. The reviewer then sees, per paired class, whether *both* builds share
an implementation of the same dangerous component — the "these two apps may be
the same malware" tell.
"""

from __future__ import annotations

from typing import Iterable

from .model import Class

# label -> (short code, human name). Short code mirrors the manifest-binding
# permission mnemonics a reviewer uses (BNL/BAS/BDA = BIND_* permissions).
_SENSITIVE: dict[str, tuple[str, str]] = {
    "Landroid/service/notification/NotificationListenerService;": ("BNL", "BindNotificationListener"),
    "Landroid/accessibilityservice/AccessibilityService;": ("BAS", "BindAccessibilityService"),
    "Landroid/app/admin/DeviceAdminReceiver;": ("BDA", "BindDeviceAdmin"),
    "Landroid/inputmethodservice/InputMethodService;": ("BIM", "BindInputMethod"),
    "Landroid/service/autofill/AutofillService;": ("BAF", "BindAutofill"),
    "Landroid/telephony/SmsMessage;": ("SMS", "SmsHandling"),
}

# Generic component roots — not inherently malicious, but the "services and
# receivers" the reviewer asked to see enumerated alongside the sensitive ones.
_GENERIC: dict[str, tuple[str, str]] = {
    "Landroid/app/Service;": ("SVC", "Service"),
    "Landroid/content/BroadcastReceiver;": ("RCV", "BroadcastReceiver"),
    "Landroid/content/ContentProvider;": ("PRV", "ContentProvider"),
    "Landroid/app/Application;": ("APP", "Application"),
}

_ALL: dict[str, tuple[str, str]] = {**_SENSITIVE, **_GENERIC}

# Codes considered malware-relevant (drives the "shared sensitive impl" headline).
SENSITIVE_CODES = frozenset(code for code, _ in _SENSITIVE.values())


def _walk_bases(cls: Class, supers: dict[str, str | None]) -> set[str]:
    """Every base descriptor reachable from `cls` via superclass chain +
    declared interfaces (transitive through app-local classes)."""
    reached: set[str] = set()
    cur = cls.superclass
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        reached.add(cur)
        cur = supers.get(cur)  # advance only while the super is an app-local class
    reached.update(cls.interfaces)
    return reached


def component_labels(classes: Iterable[Class]) -> dict[str, set[str]]:
    """descriptor -> set of component short-codes it implements (e.g. {"BAS"}).

    Only classes reaching at least one known base appear in the mapping."""
    classes = list(classes)
    supers = {c.descriptor: c.superclass for c in classes}
    out: dict[str, set[str]] = {}
    for c in classes:
        codes = {_ALL[b][0] for b in _walk_bases(c, supers) if b in _ALL}
        if codes:
            out[c.descriptor] = codes
    return out


def merge_labels(*maps: dict[str, set[str]]) -> dict[str, set[str]]:
    """Union component maps from several apps into one descriptor lookup."""
    merged: dict[str, set[str]] = {}
    for m in maps:
        for desc, codes in m.items():
            merged.setdefault(desc, set()).update(codes)
    return merged


def code_name(code: str) -> str:
    for c, name in _ALL.values():
        if c == code:
            return name
    return code
