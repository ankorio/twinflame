from __future__ import annotations

from pathlib import Path
from typing import Optional

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


def _merge_dexes(apk) -> list[Class]:
    return _merge_dexes_from_blobs(list(apk.get_all_dex()))


def _merge_dexes_from_blobs(blobs: list[bytes]) -> list[Class]:
    from androguard.core.dex import DEX

    dexes = [DEX(b) for b in blobs]
    # Build a whole-app cross-reference graph once. It gives us, per method,
    # both its call targets (B1 anchors) and its incoming-call count (xref),
    # plus the string→class map (B2 anchors). create_xref() is the expensive
    # step; it's acceptable for a POC and degrades gracefully if unavailable.
    analysis = _build_analysis(dexes)
    strings_by_class = _strings_by_class(analysis)

    seen_descriptors: set[str] = set()
    out: list[Class] = []
    for dex in dexes:
        for cdi in dex.get_classes():
            descriptor = cdi.get_name()
            if descriptor in seen_descriptors:
                continue
            seen_descriptors.add(descriptor)
            out.append(_wrap_class(cdi, analysis, strings_by_class.get(descriptor, ())))
    return out


def _build_analysis(dexes):
    """Construct an androguard Analysis with xrefs, or None on any failure."""
    try:
        from androguard.core.analysis.analysis import Analysis

        analysis = Analysis()
        for dex in dexes:
            analysis.add(dex)
        analysis.create_xref()
        return analysis
    except Exception:
        # Version skew or pathological input: fall back to no enrichment.
        # calls/strings stay empty and xref_count stays 0 (pre-M1.2 behavior).
        return None


def _strings_by_class(analysis) -> dict[str, tuple[str, ...]]:
    """Map class descriptor -> the string constants referenced from it."""
    if analysis is None:
        return {}
    acc: dict[str, set[str]] = {}
    try:
        strings = analysis.get_strings()
    except Exception:
        return {}
    for sa in strings:
        try:
            value = sa.get_value()
        except Exception:
            continue
        if value is None:
            continue
        for ref in _safe_xref_iter(sa, "get_xref_from"):
            cls_desc = _ref_class_descriptor(ref)
            if cls_desc:
                acc.setdefault(cls_desc, set()).add(value)
    return {k: tuple(sorted(v)) for k, v in acc.items()}


def _safe_xref_iter(obj, attr):
    """Yield xref entries from an androguard analysis object, tolerantly."""
    fn = getattr(obj, attr, None)
    if fn is None:
        return
    try:
        entries = fn()
    except Exception:
        return
    for entry in entries or ():
        yield entry


def _ref_class_descriptor(ref) -> str | None:
    """Pull the class descriptor out of an xref tuple/object, defensively.

    androguard xref entries are typically (ClassAnalysis, MethodAnalysis,
    offset) tuples, but exact shapes vary by version — so we probe.
    """
    candidate = ref[0] if isinstance(ref, (tuple, list)) and ref else ref
    for getter in ("get_vm_class", "get_class"):
        fn = getattr(candidate, getter, None)
        if fn is not None:
            try:
                cls = fn()
                name = cls.get_name() if hasattr(cls, "get_name") else None
                if name:
                    return name
            except Exception:
                pass
    # Some shapes expose the class name directly on the analysis object.
    for getter in ("get_name", "class_name"):
        fn = getattr(candidate, getter, None)
        try:
            name = fn() if callable(fn) else fn
        except Exception:
            name = None
        if isinstance(name, str) and name.startswith("L"):
            return name
    return None


def _wrap_class(cdi, analysis=None, strings: tuple[str, ...] = ()) -> Class:
    descriptor = cdi.get_name()
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

    methods = tuple(_wrap_method(m, analysis) for m in cdi.get_methods())
    fields = tuple(_wrap_field(f) for f in cdi.get_fields())

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


NEW_INSTANCE_OPCODE = 0x22


def _new_instance_target(ins) -> str | None:
    """Class descriptor a `new-instance` instruction targets, or None.

    `get_operands(0)` returns operand tuples; the type/string-reference one
    is `(kind, index, resolved_string)` — pull the resolved descriptor
    directly rather than parsing `get_output()` text.
    """
    try:
        for operand in ins.get_operands(0):
            if isinstance(operand, tuple) and len(operand) == 3 and isinstance(operand[2], str):
                return operand[2]
    except Exception:
        pass
    return None


def _wrap_method(em, analysis=None) -> Method:
    name = em.get_name()
    descriptor = em.get_descriptor()  # e.g. "(II)V"
    access = AccessFlag(em.get_access_flags() & 0x3FFFF)
    arg_count, return_type = _parse_proto(descriptor)

    opcodes = bytearray()
    opcode_xor = 0
    instantiates: list[str] = []
    code = em.get_code()
    if code is not None:
        try:
            for ins in em.get_instructions():
                op = ins.get_op_value()
                if op is None:
                    continue
                opcodes.append(op & 0xFF)
                opcode_xor ^= op & 0xFF
                if op == NEW_INSTANCE_OPCODE:
                    target = _new_instance_target(ins)
                    if target:
                        instantiates.append(target)
        except Exception:
            # Some pathological DEX files have unparseable instruction
            # streams. Treat the body as empty rather than failing the load.
            opcodes = bytearray()
            opcode_xor = 0
            instantiates = []

    calls, xref_count = _method_calls_and_xrefs(em, analysis)

    return Method(
        name=name,
        descriptor=descriptor,
        access=access,
        arg_count=arg_count,
        return_type=return_type,
        xref_count=xref_count,
        bytecode=bytes(opcodes),
        instr_count=len(opcodes),
        opcode_xor=opcode_xor,
        calls=calls,
        instantiates=tuple(instantiates),
    )


def _method_calls_and_xrefs(em, analysis) -> tuple[tuple[str, ...], int]:
    """Return (call-target refs, incoming-call count) for a method.

    Both come from the androguard Analysis cross-reference graph. Defensive
    throughout: any version-shape mismatch yields ((), 0) rather than failing
    the whole load.
    """
    if analysis is None:
        return (), 0
    mca = None
    try:
        mca = analysis.get_method(em)
    except Exception:
        mca = None
    if mca is None:
        return (), 0

    calls: list[str] = []
    for entry in _safe_xref_iter(mca, "get_xref_to"):
        ref = _callee_ref(entry)
        if ref:
            calls.append(ref)

    xref_count = sum(1 for _ in _safe_xref_iter(mca, "get_xref_from"))
    return tuple(calls), xref_count


def _callee_ref(entry) -> str | None:
    """Build "Lcls;->name(desc)ret" for a callee in an xref_to entry.

    An xref_to entry is (ClassAnalysis, MethodAnalysis, offset); the callee is
    the middle element. androguard's MethodAnalysis exposes the name/class/
    descriptor as both properties and `get_*` accessors depending on version,
    so we probe both forms.
    """
    if isinstance(entry, (tuple, list)):
        target = entry[1] if len(entry) >= 2 else (entry[0] if entry else None)
    else:
        target = entry
    if target is None:
        return None
    name = _read_attr(target, "name", "get_name")
    if not name:
        return None
    cls = _read_attr(target, "class_name", "get_class_name")
    desc = _read_attr(target, "descriptor", "get_descriptor")
    return f"{cls}->{name}{desc}"


def _read_attr(obj, *names) -> str:
    """Read the first attribute that resolves to a non-empty string.

    Each name may be a plain attribute/property or a zero-arg method.
    """
    for n in names:
        val = getattr(obj, n, None)
        if val is None:
            continue
        try:
            val = val() if callable(val) else val
        except Exception:
            continue
        if isinstance(val, str) and val:
            return val
    return ""


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
