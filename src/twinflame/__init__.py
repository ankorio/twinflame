"""twinflame — DEX-level Android APK class-diffing engine."""

from .api import diff, filter, load
from .model import (
    App,
    Class,
    DiffOptions,
    Field,
    Match,
    Method,
    MethodMatch,
    Pool,
    Signature,
)
from .prepare import PreparedRecord, load_record, migrate_record, prepare, save

# WIP / experimental — Tier-1 containment kinship scoring. The self-containment
# anchor is reliable; family-vs-benign thresholds are uncalibrated and depend on
# the unbuilt library dictionary (M3.3). See the batch-scoring design.
from .score import ContainmentResult, containment

__all__ = [
    "App",
    "Class",
    "DiffOptions",
    "Field",
    "Match",
    "Method",
    "MethodMatch",
    "ContainmentResult",
    "Pool",
    "PreparedRecord",
    "Signature",
    "containment",
    "diff",
    "filter",
    "load",
    "load_record",
    "migrate_record",
    "prepare",
    "save",
]

# Single source of truth is the [project].version in pyproject.toml; read it
# back from the installed package metadata so the two never drift. Falls back
# when running from an uninstalled source tree.
from importlib.metadata import PackageNotFoundError, version as _pkg_version  # noqa: E402

try:
    __version__ = _pkg_version("twinflame")
except PackageNotFoundError:  # source checkout without an install
    __version__ = "0.0.0+unknown"

del _pkg_version, PackageNotFoundError
