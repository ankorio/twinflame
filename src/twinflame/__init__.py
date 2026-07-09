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
from .prepare import PreparedRecord, load_record, prepare, save

__all__ = [
    "App",
    "Class",
    "DiffOptions",
    "Field",
    "Match",
    "Method",
    "MethodMatch",
    "Pool",
    "PreparedRecord",
    "Signature",
    "diff",
    "filter",
    "load",
    "load_record",
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
