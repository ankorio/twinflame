from __future__ import annotations

from pathlib import Path
from typing import Optional

from .model import AccessFlag, App, Class, Field, ManifestInfo, Method


def load(path: str | Path, *, redex_normalize: bool = False) -> App:
    """Parse an APK into an `App` view.

    Multi-DEX is resolved into a single unioned class list (first-wins on
    descriptor collision, matching ART semantics).
    """
    # Local imports keep androguard out of the import graph of callers that
    # use the API only to consume pre-built `Class` objects (e.g. tests).
    from androguard.core.apk import APK

    target = Path(path)
    if redex_normalize:
        from . import normalize as _normalize

        target = _normalize.normalize(target)

    apk = APK(str(target))
    classes = _merge_dexes(apk)
    manifest = _parse_manifest(apk)
    return App(path=target, classes=tuple(classes), manifest=manifest)


def _merge_dexes(apk) -> list[Class]:
    from androguard.core.dex import DEX

    seen_descriptors: set[str] = set()
    out: list[Class] = []
    for dex_bytes in apk.get_all_dex():
        dex = DEX(dex_bytes)
        for cdi in dex.get_classes():
            descriptor = cdi.get_name()
            if descriptor in seen_descriptors:
                continue
            seen_descriptors.add(descriptor)
            out.append(_wrap_class(cdi))
    return out


def _wrap_class(cdi) -> Class:
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

    methods = tuple(_wrap_method(m) for m in cdi.get_methods())
    fields = tuple(_wrap_field(f) for f in cdi.get_fields())

    is_inner = "$" in name
    is_synthetic = bool(access & AccessFlag.SYNTHETIC)
    is_external = all(m.bytecode == b"" for m in methods) if methods else True

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
        strings=(),  # not collected in v1; see _hot.py for the seam
    )


def _wrap_method(em) -> Method:
    name = em.get_name()
    descriptor = em.get_descriptor()  # e.g. "(II)V"
    access = AccessFlag(em.get_access_flags() & 0x3FFFF)
    arg_count, return_type = _parse_proto(descriptor)

    opcodes = bytearray()
    opcode_xor = 0
    code = em.get_code()
    if code is not None:
        try:
            for ins in em.get_instructions():
                op = ins.get_op_value()
                if op is None:
                    continue
                opcodes.append(op & 0xFF)
                opcode_xor ^= op & 0xFF
        except Exception:
            # Some pathological DEX files have unparseable instruction
            # streams. Treat the body as empty rather than failing the load.
            opcodes = bytearray()
            opcode_xor = 0

    return Method(
        name=name,
        descriptor=descriptor,
        access=access,
        arg_count=arg_count,
        return_type=return_type,
        xref_count=0,
        bytecode=bytes(opcodes),
        instr_count=len(opcodes),
        opcode_xor=opcode_xor,
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
    return _count_params(params), ret


def _count_params(params: str) -> int:
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
