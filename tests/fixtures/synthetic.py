"""Synthetic Class/Method/Field factories for tests.

We build `twinflame.model.Class` instances directly from high-level Python
specs and feed them through cluster -> signature -> accurate -> api. This
bypasses the on-disk DEX/APK format (see build_dex.py) but exercises every
algorithm the engine implements. No binary fixtures are committed.

Loader-side testing of androguard wrapping is covered by tests/unit/
test_loader.py with a tiny real DEX once that fixture lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from twinflame.model import AccessFlag, Class, Field, Method


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type_desc: str
    access: AccessFlag = AccessFlag.PUBLIC


@dataclass(frozen=True)
class MethodSpec:
    name: str
    descriptor: str = "()V"
    access: AccessFlag = AccessFlag.PUBLIC
    arg_count: int = 0
    return_type: str = "V"
    xref_count: int = 0
    bytecode: bytes = b"\x0e"  # return-void
    instr_count: int = 1
    calls: tuple[str, ...] = ()
    instantiates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClassSpec:
    descriptor: str
    source_file: str | None = None
    access: AccessFlag = AccessFlag.PUBLIC
    is_inner: bool = False
    is_synthetic: bool = False
    is_external: bool = False
    methods: tuple[MethodSpec, ...] = ()
    fields: tuple[FieldSpec, ...] = ()
    strings: tuple[str, ...] = ()
    superclass: str | None = None
    interfaces: tuple[str, ...] = ()


def _descriptor_to_package_name(desc: str) -> tuple[str, str]:
    inner = desc.lstrip("L").rstrip(";")
    if "/" in inner:
        pkg_path, _, name = inner.rpartition("/")
        return pkg_path.replace("/", "."), name
    return "", inner


def make_method(spec: MethodSpec) -> Method:
    bc = bytes(spec.bytecode)
    opcode_xor = 0
    for b in bc:
        opcode_xor ^= b
    return Method(
        name=spec.name,
        descriptor=spec.descriptor,
        access=spec.access,
        arg_count=spec.arg_count,
        return_type=spec.return_type,
        xref_count=spec.xref_count,
        bytecode=bc,
        instr_count=spec.instr_count if spec.instr_count else len(bc),
        opcode_xor=opcode_xor,
        calls=spec.calls,
        instantiates=spec.instantiates,
    )


def make_field(spec: FieldSpec) -> Field:
    return Field(name=spec.name, type_desc=spec.type_desc, access=spec.access)


def make_class(spec: ClassSpec) -> Class:
    pkg, name = _descriptor_to_package_name(spec.descriptor)
    return Class(
        descriptor=spec.descriptor,
        package=pkg,
        name=name,
        source_file=spec.source_file,
        access=spec.access,
        is_inner=spec.is_inner,
        is_synthetic=spec.is_synthetic,
        is_external=spec.is_external,
        methods=tuple(make_method(m) for m in spec.methods),
        fields=tuple(make_field(f) for f in spec.fields),
        strings=spec.strings,
        superclass=spec.superclass,
        interfaces=spec.interfaces,
    )


def make_classes(specs: list[ClassSpec]) -> list[Class]:
    return [make_class(s) for s in specs]


def standard_methods() -> tuple[MethodSpec, ...]:
    return (
        MethodSpec(
            name="<init>",
            descriptor="()V",
            access=AccessFlag.PUBLIC | AccessFlag.CONSTRUCTOR,
            bytecode=bytes([0x70, 0x0e]),
            instr_count=2,
        ),
        MethodSpec(
            name="getValue",
            descriptor="()I",
            return_type="I",
            bytecode=bytes([0x12, 0x0f]),
            instr_count=2,
        ),
        MethodSpec(
            name="setValue",
            descriptor="(I)V",
            arg_count=1,
            bytecode=bytes([0x59, 0x0e]),
            instr_count=2,
        ),
    )


def standard_fields() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(name="value", type_desc="I"),
        FieldSpec(name="name", type_desc="Ljava/lang/String;"),
    )


def vendor_app(
    package: str = "com.acme.app",
    n_classes: int = 8,
) -> list[Class]:
    """A small synthetic 'vendor app' with structurally-varied classes.

    Each class has a different method/field shape so signatures genuinely
    differ — mirrors real apps where every class has distinct structure.
    """
    specs: list[ClassSpec] = []
    base_pkg_path = "L" + package.replace(".", "/")
    for i in range(n_classes):
        # Vary structure: instr count, method count, field count, arg counts
        methods = (
            MethodSpec(
                name="<init>",
                descriptor="()V",
                access=AccessFlag.PUBLIC | AccessFlag.CONSTRUCTOR,
                bytecode=bytes([0x70] * (1 + i % 3) + [0x0E]),
                instr_count=2 + i % 3,
            ),
            MethodSpec(
                name="op0",
                descriptor=f"({'I' * (i % 3)})I",
                return_type="I",
                arg_count=i % 3,
                bytecode=bytes(
                    [0x12 + (j % 4) for j in range(2 + i)] + [0x0F]
                ),
                instr_count=3 + i,
            ),
            MethodSpec(
                name="op1",
                descriptor="()V",
                bytecode=bytes([0x5A + (i % 5)] * (1 + i % 2) + [0x0E]),
                instr_count=2 + i % 2,
            ),
        )
        fields = tuple(
            FieldSpec(name=f"f{j}", type_desc=("I", "J", "Ljava/lang/String;")[j % 3])
            for j in range(1 + i % 3)
        )
        specs.append(
            ClassSpec(
                descriptor=f"{base_pkg_path}/Class{i};",
                source_file=f"Class{i}.java",
                methods=methods,
                fields=fields,
            )
        )
    return make_classes(specs)


def mutate_method_bytecode(c: Class, method_name: str, new_bytecode: bytes) -> Class:
    """Return a new Class with one method's bytecode swapped (others untouched)."""
    new_methods = []
    for m in c.methods:
        if m.name == method_name:
            opcode_xor = 0
            for b in new_bytecode:
                opcode_xor ^= b
            new_methods.append(
                Method(
                    name=m.name,
                    descriptor=m.descriptor,
                    access=m.access,
                    arg_count=m.arg_count,
                    return_type=m.return_type,
                    xref_count=m.xref_count,
                    bytecode=new_bytecode,
                    instr_count=len(new_bytecode),
                    opcode_xor=opcode_xor,
                    calls=m.calls,
                    instantiates=m.instantiates,
                )
            )
        else:
            new_methods.append(m)
    return Class(
        descriptor=c.descriptor,
        package=c.package,
        name=c.name,
        source_file=c.source_file,
        access=c.access,
        is_inner=c.is_inner,
        is_synthetic=c.is_synthetic,
        is_external=c.is_external,
        methods=tuple(new_methods),
        fields=c.fields,
        strings=c.strings,
        superclass=c.superclass,
        interfaces=c.interfaces,
    )
