from __future__ import annotations

from pathlib import Path
from typing import Optional

from .model import ManifestInfo


def parse(apk_path: Path) -> Optional[ManifestInfo]:
    from androguard.core.apk import APK

    return parse_from_apk(APK(str(apk_path)))


def parse_from_apk(apk) -> Optional[ManifestInfo]:
    try:
        package = apk.get_package() or ""
    except Exception:
        package = ""

    activities = _safe_list(apk, "get_activities")
    services = _safe_list(apk, "get_services")
    receivers = _safe_list(apk, "get_receivers")
    providers = _safe_list(apk, "get_providers")
    return ManifestInfo(
        package=package,
        activities=tuple(activities),
        services=tuple(services),
        receivers=tuple(receivers),
        providers=tuple(providers),
    )


def _safe_list(apk, attr: str) -> list[str]:
    fn = getattr(apk, attr, None)
    if fn is None:
        return []
    try:
        result = fn()
    except Exception:
        return []
    return [str(x) for x in (result or [])]


def suggest_package_prefix(m: ManifestInfo) -> str:
    """Pick the longest common dotted prefix across all declared components.

    Falls back to the manifest's `package` attribute when there's no common
    prefix or no components are declared.
    """
    components = list(m.activities) + list(m.services) + list(m.receivers) + list(m.providers)
    components = [_strip_leading_dot(c, m.package) for c in components if c]
    if not components:
        return m.package
    prefix = _longest_common_dotted_prefix(components)
    return prefix or m.package


def _strip_leading_dot(component: str, package: str) -> str:
    # Manifest entries often use the shorthand ".Activity" meaning
    # "<package>.Activity". Expand them so prefix computation is honest.
    if component.startswith(".") and package:
        return package + component
    return component


def _longest_common_dotted_prefix(names: list[str]) -> str:
    if not names:
        return ""
    split_names = [n.split(".") for n in names]
    common: list[str] = []
    for parts in zip(*split_names):
        if all(p == parts[0] for p in parts):
            common.append(parts[0])
        else:
            break
    # Drop the trailing class-name segment if it survived (i.e., when all
    # components are the same single class) — we want a package prefix.
    if len(common) == len(split_names[0]):
        common = common[:-1]
    return ".".join(common)
