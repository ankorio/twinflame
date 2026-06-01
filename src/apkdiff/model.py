from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag
from pathlib import Path
from typing import Optional


class AccessFlag(IntFlag):
    PUBLIC = 0x1
    PRIVATE = 0x2
    PROTECTED = 0x4
    STATIC = 0x8
    FINAL = 0x10
    SYNCHRONIZED = 0x20
    VOLATILE = 0x40
    BRIDGE = 0x40
    TRANSIENT = 0x80
    VARARGS = 0x80
    NATIVE = 0x100
    INTERFACE = 0x200
    ABSTRACT = 0x400
    STRICT = 0x800
    SYNTHETIC = 0x1000
    ANNOTATION = 0x2000
    ENUM = 0x4000
    CONSTRUCTOR = 0x10000
    DECLARED_SYNCHRONIZED = 0x20000


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    type_desc: str
    access: AccessFlag


@dataclass(frozen=True, slots=True)
class Method:
    name: str
    descriptor: str
    access: AccessFlag
    arg_count: int
    return_type: str
    xref_count: int
    bytecode: bytes
    instr_count: int
    opcode_xor: int
    # Full references of the methods this one invokes (e.g.
    # "Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V"). Populated by
    # the loader from the call graph; empty for synthetic/test classes. Feeds
    # the B1 framework-call anchors in anchor.py.
    calls: tuple[str, ...] = ()

    @property
    def order_key(self) -> int:
        # 16-bit sort key for Stage-3 method-order normalization:
        # high byte = instruction count (clamped), low byte = XOR-fold of opcodes.
        return ((self.instr_count & 0xFF) << 8) | (self.opcode_xor & 0xFF)


@dataclass(frozen=True, slots=True)
class Class:
    descriptor: str
    package: str
    name: str
    source_file: Optional[str]
    access: AccessFlag
    is_inner: bool
    is_synthetic: bool
    is_external: bool
    methods: tuple[Method, ...]
    fields: tuple[Field, ...]
    strings: tuple[str, ...]

    @property
    def info(self) -> str:
        src = self.source_file or "<unknown>"
        return f"{self.package}: {self.name} - {src}"

    @property
    def total_instructions(self) -> int:
        return sum(m.instr_count for m in self.methods)


@dataclass(frozen=True, slots=True)
class ManifestInfo:
    package: str
    activities: tuple[str, ...]
    services: tuple[str, ...]
    receivers: tuple[str, ...]
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class App:
    path: Path
    classes: tuple[Class, ...]
    manifest: Optional[ManifestInfo]


@dataclass(frozen=True, slots=True)
class Signature:
    cls: int
    fld: int
    mth: int
    code: int

    @property
    def combined(self) -> int:
        return (self.cls << 96) | (self.fld << 64) | (self.mth << 32) | self.code

    def hamming(self, other: "Signature") -> int:
        return (
            (self.cls ^ other.cls).bit_count()
            + (self.fld ^ other.fld).bit_count()
            + (self.mth ^ other.mth).bit_count()
            + (self.code ^ other.code).bit_count()
        )

    @classmethod
    def from_combined(cls, value: int) -> "Signature":
        return cls(
            cls=(value >> 96) & 0xFFFFFFFF,
            fld=(value >> 64) & 0xFFFFFFFF,
            mth=(value >> 32) & 0xFFFFFFFF,
            code=value & 0xFFFFFFFF,
        )


@dataclass(frozen=True, slots=True)
class MethodMatch:
    """One method-level verdict inside a paired class.

    `status` is one of: "matched" (identical, score 1.0), "modified" (paired
    but the body changed), "added" (only in rhs), "deleted" (only in lhs).
    """

    lhs: Optional[Method]
    rhs: Optional[Method]
    score: float
    status: str

    @property
    def name(self) -> str:
        m = self.lhs or self.rhs
        return m.name if m else "<?>"


@dataclass(frozen=True, slots=True)
class Match:
    lhs: Optional[Class]
    rhs: Optional[Class]
    distance: float
    breakdown: dict[str, float] = field(default_factory=dict)
    method_matches: tuple[MethodMatch, ...] = ()

    @property
    def is_added(self) -> bool:
        return self.lhs is None and self.rhs is not None

    @property
    def is_deleted(self) -> bool:
        return self.lhs is not None and self.rhs is None

    @property
    def is_paired(self) -> bool:
        return self.lhs is not None and self.rhs is not None

    @property
    def changed_methods(self) -> tuple[MethodMatch, ...]:
        """Method verdicts that aren't a clean 1:1 identical match."""
        return tuple(mm for mm in self.method_matches if mm.status != "matched")


@dataclass(slots=True)
class Pool:
    key: str
    lhs: list[Class]
    rhs: list[Class]


@dataclass(frozen=True, slots=True)
class DiffOptions:
    inner_skipping: bool = False
    external_skipping: bool = False
    synthetic_skipping: bool = True
    find_obfuscated_packages: bool = False
    min_inst_size_threshold: int = 5
    top_match_threshold: int = 3
    buckets: int = 16
    jobs: int = 1
    use_source_file: bool = False
    use_strings: bool = False
    cluster: bool = True
    anchoring: bool = True
    progress: bool = False

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "DiffOptions":
        if not raw:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in raw.items() if k in known}
        return cls(**kwargs)
