from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple, Optional

from .model import AccessFlag, App, Class, Field, ManifestInfo, Method


class LoadError(Exception):
    """A user-facing input error: missing path, not an APK/DEX, or corrupt file.

    Raised instead of leaking an androguard/apkInspector/zip traceback so the CLI
    can print a clean message and exit non-zero.
    """


def load(path: str | Path, *, redex_normalize: bool = False) -> App:
    """Parse an APK into an `App` view.

    Multi-DEX is resolved into a single unioned class list (first-wins on
    descriptor collision, matching ART semantics). Raises `LoadError` on a
    missing/unreadable/corrupt APK.
    """
    # Local imports keep androguard out of the import graph of callers that
    # use the API only to consume pre-built `Class` objects (e.g. tests).
    from androguard.core.apk import APK

    target = Path(path)
    if not target.exists():
        raise LoadError(f"file not found: {target}")
    if not target.is_file():
        raise LoadError(f"not a file (is it a directory of .dex? use --dex): {target}")
    if redex_normalize:
        from . import normalize as _normalize

        target = _normalize.normalize(target)

    try:
        apk = APK(str(target))
        blobs = list(apk.get_all_dex())
    except Exception as e:  # androguard/apkInspector: bad zip, no EOCD, etc.
        raise LoadError(f"not a valid APK ({target.name}): {e}") from e
    if not blobs:
        raise LoadError(f"APK contains no DEX ({target.name}) — nothing to diff")
    try:
        classes = _merge_dexes_from_blobs(blobs)
    except Exception as e:
        raise LoadError(f"could not parse DEX inside {target.name}: {e}") from e
    manifest = _parse_manifest(apk)
    return App(path=target, classes=tuple(classes), manifest=manifest)


def load_dex(paths, *, label: str | None = None) -> App:
    """Parse one or more raw ``.dex`` blobs into an `App`, no APK required.

    `paths` is a path or iterable of paths, each either a ``.dex`` file or a
    directory (searched recursively for ``*.dex``). This is the entry point for
    **dumped / extracted DEX** (e.g. pulled from memory, a runtime dump, or an
    unpacked payload) where there is no surrounding APK — so `manifest` is
    ``None`` (features that need it, like ``--auto-package``, are unavailable;
    pass ``--app-package`` explicitly for provenance instead).

    Multi-DEX is unioned first-wins on descriptor collision, exactly like APK
    loading, so a directory of `classes.dex, classes2.dex, …` behaves like the
    APK it came from. Raises `LoadError` on a missing directory/file or a corrupt
    DEX.
    """
    files = _collect_dex_files(paths)
    if not files:
        raise LoadError(f"no .dex files found in: {_fmt_paths(paths)}")
    for f in files:
        if not Path(f).exists():
            raise LoadError(f"file not found: {f}")
        if not Path(f).is_file():
            raise LoadError(f"not a file: {f}")
    try:
        blobs = [Path(f).read_bytes() for f in files]
        classes = _merge_dexes_from_blobs(blobs)
    except Exception as e:
        raise LoadError(f"could not parse DEX ({len(files)} file(s)): {e}") from e
    where = Path(label) if label else Path(files[0])
    return App(path=where, classes=tuple(classes), manifest=None)


def _fmt_paths(paths) -> str:
    if isinstance(paths, (str, Path)):
        return str(paths)
    return ", ".join(str(p) for p in paths)


def _collect_dex_files(paths) -> list[Path]:
    """Expand paths (files and/or directories) into a sorted, de-duplicated list
    of ``.dex`` files. Directories are searched recursively; explicit files are
    taken as-is (any extension) so an oddly-named dump still works."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        p = Path(p)
        candidates = sorted(p.rglob("*.dex")) if p.is_dir() else [p]
        for c in candidates:
            rc = c.resolve()
            if rc not in seen:
                seen.add(rc)
                out.append(c)
    return out


_LENIENCY_PATCHED = False


def _patch_dex_leniency() -> None:
    """Make androguard tolerate hidden-API flag values it doesn't model.

    Memory-dumped / repacked DEX commonly carry `HiddenApiClassDataItem` domain
    flags outside androguard's strict enum (values 4/6 vs the modeled 0/1/2),
    which otherwise aborts parsing of the *entire* dex. twinflame never reads
    these flags, so we install a lenient `_missing_` that maps any unknown value
    to the enum's zero member. Idempotent; applied lazily at first parse."""
    global _LENIENCY_PATCHED
    if _LENIENCY_PATCHED:
        return
    _LENIENCY_PATCHED = True
    try:
        from androguard.core.dex import HiddenApiClassDataItem

        for name in ("DomapiApiFlag", "RestrictionApiFlag"):
            enum_cls = getattr(HiddenApiClassDataItem, name, None)
            if enum_cls is not None:
                enum_cls._missing_ = classmethod(lambda cls, value: list(cls)[0])
    except Exception:
        pass  # older/newer androguard without this section — nothing to patch


def _parse_dexes(blobs: list[bytes]):
    """Parse DEX blobs, skipping any that fail (partial memory dumps have corrupt
    sections). Warns with the skip count; raises only if *nothing* parses."""
    from androguard.core.dex import DEX

    _patch_dex_leniency()
    dexes, skipped = [], 0
    for b in blobs:
        try:
            dexes.append(DEX(b))
        except Exception:
            skipped += 1
    if skipped:
        print(f"loader: skipped {skipped}/{len(blobs)} unparseable dex blob(s)", file=sys.stderr)
    if not dexes:
        raise LoadError(f"no parseable DEX among {len(blobs)} blob(s)")
    return dexes


# Bump when the extraction semantics change what lands in Method/Class fields
# (folded into prepare.ALGO_VERSION so stale prepared records regenerate).
# 2: direct operand extraction replaced androguard Analysis; array-receiver
#    calls are now kept faithfully ("[LA;->clone()..." instead of the old
#    layer's "LA;->..." rewrite, primitive-array clones no longer dropped).
EXTRACTION_VERSION = 2

# Opcodes whose reference operand the extraction walk resolves. The invoke set
# mirrors androguard's create_xref exactly (invoke-kind 0x6E-0x72 and
# invoke-kind/range 0x74-0x78) so call refs stay comparable with records
# produced by the old Analysis-based loader.
INVOKE_OPCODES = frozenset(range(0x6E, 0x73)) | frozenset(range(0x74, 0x79))
CONST_STRING_OPCODES = frozenset({0x1A, 0x1B})  # const-string, const-string/jumbo
NEW_INSTANCE_OPCODE = 0x22


class _RawMethod(NamedTuple):
    """Everything one instruction walk yields for a method, before the
    app-wide incoming-call tally (xref_count) is known."""

    name: str
    descriptor: str
    access: AccessFlag
    bytecode: bytes
    opcode_xor: int
    calls: tuple[str, ...]
    instantiates: tuple[str, ...]
    strings: tuple[str, ...]


def _merge_dexes_from_blobs(blobs: list[bytes]) -> list[Class]:
    dexes = _parse_dexes(blobs)
    # One instruction walk per method extracts everything the models need:
    # opcodes, invoke targets (B1 anchors / feature deltas), const-strings
    # (B2 anchors), and new-instance targets. Incoming-call counts need the
    # whole app, so Class assembly happens in a second pass once every call
    # edge is tallied. This replaces androguard's Analysis/create_xref layer,
    # which recomputed the same three facts ~9x slower via a full object
    # graph — and silently zeroed them all whenever it failed to build.
    seen_descriptors: set[str] = set()
    pending: list[tuple[object, str, list[_RawMethod]]] = []
    call_tally: Counter[str] = Counter()
    for dex in dexes:
        for cdi in dex.get_classes():
            descriptor = cdi.get_name()
            if descriptor in seen_descriptors:
                continue
            seen_descriptors.add(descriptor)
            raws = [_extract_method(m) for m in cdi.get_methods()]
            for raw in raws:
                call_tally.update(raw.calls)
            pending.append((cdi, descriptor, raws))
    return [_wrap_class(cdi, desc, raws, call_tally) for cdi, desc, raws in pending]


def _extract_method(em) -> _RawMethod:
    """Walk a method's instructions once, collecting opcodes and operands."""
    opcodes = bytearray()
    opcode_xor = 0
    calls: list[str] = []
    instantiates: list[str] = []
    strings: list[str] = []
    if em.get_code() is not None:
        try:
            for ins in em.get_instructions():
                op = ins.get_op_value()
                if op is None:
                    continue
                opcodes.append(op & 0xFF)
                opcode_xor ^= op & 0xFF
                if op in INVOKE_OPCODES:
                    ref = _operand_ref(ins)
                    if ref:
                        calls.append(ref)
                elif op == NEW_INSTANCE_OPCODE:
                    ref = _operand_ref(ins)
                    if ref:
                        instantiates.append(ref)
                elif op in CONST_STRING_OPCODES:
                    ref = _operand_ref(ins)
                    if ref is not None:  # keep "" — a const-string can be empty
                        strings.append(ref)
        except Exception:
            # Some pathological DEX files have unparseable instruction
            # streams. Treat the body as empty rather than failing the load.
            opcodes = bytearray()
            opcode_xor = 0
            calls = []
            instantiates = []
            strings = []
    return _RawMethod(
        name=em.get_name(),
        descriptor=em.get_descriptor(),
        access=AccessFlag(em.get_access_flags() & 0x3FFFF),
        bytecode=bytes(opcodes),
        opcode_xor=opcode_xor,
        calls=tuple(calls),
        instantiates=tuple(instantiates),
        strings=tuple(strings),
    )


def _operand_ref(ins) -> str | None:
    """The resolved reference operand of an instruction, or None.

    `get_operands(0)` returns register tuples plus one table-reference tuple
    `(kind, index, resolved)`; the resolved element is "Lcls;->name(args)ret"
    for invoke-*, the class descriptor for new-instance, and the value for
    const-string.
    """
    try:
        for operand in ins.get_operands(0):
            if isinstance(operand, tuple) and len(operand) == 3 and isinstance(operand[2], str):
                return operand[2]
    except Exception:
        pass
    return None


def _wrap_class(cdi, descriptor: str, raws: list[_RawMethod], call_tally: Counter) -> Class:
    package, name = _split_descriptor(descriptor)
    access = AccessFlag(cdi.get_access_flags() & 0x3FFFF)
    # `cdi.get_source_ext()` routes through `CM.decompiler_ob` which is
    # often `None` on a freshly-parsed DEX. Read the string table directly.
    source_file = None
    src_idx = cdi.get_source_file_idx()
    if src_idx not in (-1, 0xFFFFFFFF):
        try:
            source_file = cdi.CM.get_string(src_idx) or None
        except Exception:
            source_file = None

    # A method's own ref key is built exactly like a callee ref out of the
    # operand table ("Lcls;->name(args)ret"), so the tally lookup is exact.
    methods = tuple(
        _wrap_method(raw, call_tally.get(f"{descriptor}->{raw.name}{raw.descriptor}", 0))
        for raw in raws
    )
    fields = tuple(_wrap_field(f) for f in cdi.get_fields())
    strings = tuple(sorted({s for raw in raws for s in raw.strings}))

    is_inner = "$" in name
    is_synthetic = bool(access & AccessFlag.SYNTHETIC)
    is_external = all(m.bytecode == b"" for m in methods) if methods else True

    superclass = cdi.get_superclassname() or None
    try:
        interfaces = tuple(cdi.get_interfaces())
    except Exception:
        interfaces = ()

    return Class(
        descriptor=descriptor,
        package=package,
        name=name,
        source_file=source_file,
        access=access,
        is_inner=is_inner,
        is_synthetic=is_synthetic,
        is_external=is_external,
        methods=methods,
        fields=fields,
        strings=strings,
        superclass=superclass,
        interfaces=interfaces,
    )


def _wrap_method(raw: _RawMethod, xref_count: int) -> Method:
    arg_count, return_type = _parse_proto(raw.descriptor)
    return Method(
        name=raw.name,
        descriptor=raw.descriptor,
        access=raw.access,
        arg_count=arg_count,
        return_type=return_type,
        xref_count=xref_count,
        bytecode=raw.bytecode,
        instr_count=len(raw.bytecode),
        opcode_xor=raw.opcode_xor,
        calls=raw.calls,
        instantiates=raw.instantiates,
    )


def _wrap_field(ef) -> Field:
    return Field(
        name=ef.get_name(),
        type_desc=ef.get_descriptor(),
        access=AccessFlag(ef.get_access_flags() & 0x3FFFF),
    )


def _split_descriptor(desc: str) -> tuple[str, str]:
    inner = desc.lstrip("L").rstrip(";")
    if "/" in inner:
        pkg, _, name = inner.rpartition("/")
        return pkg.replace("/", "."), name
    return "", inner


def _parse_proto(descriptor: str) -> tuple[int, str]:
    if not descriptor.startswith("(") or ")" not in descriptor:
        return 0, "V"
    params, _, ret = descriptor[1:].partition(")")
    return _count_params(params), ret.strip()


def _count_params(params: str) -> int:
    # androguard's get_descriptor() separates multi-arg protos with spaces
    # (its own "pretty" format, e.g. "(Ljava/lang/String; I)I", not the raw
    # compact JVM descriptor) — strip them so they aren't miscounted as
    # their own zero-width "argument".
    params = params.replace(" ", "")
    n = 0
    i = 0
    while i < len(params):
        c = params[i]
        if c == "[":
            i += 1
            continue
        if c == "L":
            i = params.index(";", i) + 1
        else:
            i += 1
        n += 1
    return n


def _parse_manifest(apk) -> Optional[ManifestInfo]:
    try:
        from . import manifest as _manifest

        return _manifest.parse_from_apk(apk)
    except Exception:
        return None
