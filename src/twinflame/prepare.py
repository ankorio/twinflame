"""Prepare stage — split the expensive parse+fingerprint from the cheap compare.

The one-shot `load -> diff` path re-parses each APK and recomputes every class
signature and abstract-opcode sequence at diff time. For an analysis pipeline
that compares each sample against many others (family scoring, bulk kinship),
that work should happen **once per sample** and be persisted, keyed by the
sample digest, then reused for every later comparison.

`prepare()` does exactly the per-sample work: androguard parse (via `loader`) +
per-class `Signature` + per-method abstract-opcode sequence + the anchor inputs,
returning a `PreparedRecord`. Raw bytecode is dropped — the abstract sequence is
all the compare path needs. A record is **lossless for comparison**: a diff run
off `record.classes` is bit-identical to a diff off a fresh parse (see
tests/integration/test_prepare_equiv.py).

Records are version-stamped with `ALGO_VERSION` (a hash of the signature params +
opcode table + record layout). A record whose stamp differs from the running
code's is stale and must be re-prepared — never compared against a fresh one.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .model import AccessFlag, App, Class, Field, ManifestInfo, Method, Signature
from .opcodes import CATEGORY, categorize
from .signature import (
    BUCKET_PREFIX_BITS_DEFAULT,
    CALL_WEIGHT_MULT,
    DEFAULT_SEED,
    IFACE_WEIGHT,
    N_PERMUTATIONS_DEFAULT,
    PARTIAL_BITS,
    SIGNATURE_BITS,
    SUPER_WEIGHT,
    compute_signature,
)

# Bump only for a change to the record *layout*. Algorithm changes (signature
# params, opcode table) are folded into ALGO_VERSION automatically below.
RECORD_VERSION = 1


def _algo_version() -> str:
    """Short stamp over everything that affects a prepared record's comparability:
    the signature parameters and the opcode-category table. If any of these
    change, existing records are stale and must be regenerated."""
    material = repr((
        RECORD_VERSION, PARTIAL_BITS, SIGNATURE_BITS, DEFAULT_SEED,
        SUPER_WEIGHT, IFACE_WEIGHT, CALL_WEIGHT_MULT,
        N_PERMUTATIONS_DEFAULT, BUCKET_PREFIX_BITS_DEFAULT,
    )).encode() + bytes(CATEGORY)
    return hashlib.sha256(material).hexdigest()[:16]


ALGO_VERSION = _algo_version()


@dataclass(frozen=True, slots=True)
class PreparedRecord:
    """Everything needed to compare a sample later, keyed by its digest."""

    record_version: int
    algo_version: str
    digest: str
    input_kind: str            # "apk" | "dex" | "dex-dir"
    normalized: bool
    manifest: Optional[ManifestInfo]
    classes: tuple[Class, ...]  # signature-populated, methods carry `abstract`, no bytecode

    @property
    def app(self) -> App:
        """View the record as an `App` so it drops straight into `api.diff`."""
        return App(path=Path(self.digest), classes=self.classes, manifest=self.manifest)


# --- preparation ------------------------------------------------------------

def _prepare_method(m: Method) -> Method:
    return replace(m, bytecode=b"", abstract=categorize(m.bytecode))


def _prepare_class(c: Class) -> Class:
    sig = compute_signature(c)  # computed off bytecode here, cached onto the class
    return replace(
        c,
        signature=sig,
        methods=tuple(_prepare_method(m) for m in c.methods),
    )


def prepare_app(
    app: App, *, digest: str, input_kind: str, normalized: bool = False
) -> PreparedRecord:
    """Fingerprint an already-loaded `App` into a `PreparedRecord`."""
    return PreparedRecord(
        record_version=RECORD_VERSION,
        algo_version=ALGO_VERSION,
        digest=digest,
        input_kind=input_kind,
        normalized=normalized,
        manifest=app.manifest,
        classes=tuple(_prepare_class(c) for c in app.classes),
    )


def digest_of(path: str | Path) -> str:
    """sha256 of the sample. For a .dex directory, hashes each `*.dex` in sorted
    order so the digest is stable regardless of listing order."""
    h = hashlib.sha256()
    p = Path(path)
    if p.is_dir():
        for dex in sorted(p.rglob("*.dex")):
            h.update(dex.read_bytes())
    else:
        h.update(p.read_bytes())
    return h.hexdigest()


def _input_kind(path: Path) -> str:
    if path.is_dir():
        return "dex-dir"
    if path.suffix.lower() == ".dex":
        return "dex"
    return "apk"


def prepare(
    path: str | Path, *, digest: Optional[str] = None, redex_normalize: bool = False
) -> PreparedRecord:
    """Load and fingerprint a sample (APK / .dex / dir of .dex). `digest`
    defaults to the sha256 of the input; pass the pipeline's own digest to key
    the record by it."""
    from . import loader

    p = Path(path)
    kind = _input_kind(p)
    if kind in ("dex", "dex-dir"):
        app = loader.load_dex([p])
    else:
        app = loader.load(p, redex_normalize=redex_normalize)
    return prepare_app(
        app,
        digest=digest or digest_of(p),
        input_kind=kind,
        normalized=redex_normalize,
    )


# --- serialization (JSON; base64 for the abstract-opcode bytes) -------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _method_to_dict(m: Method) -> dict:
    return {
        "name": m.name, "descriptor": m.descriptor, "access": int(m.access),
        "arg_count": m.arg_count, "return_type": m.return_type,
        "xref_count": m.xref_count, "instr_count": m.instr_count,
        "opcode_xor": m.opcode_xor, "calls": list(m.calls),
        "instantiates": list(m.instantiates),
        "abstract": _b64(m.abstract if m.abstract is not None else categorize(m.bytecode)),
    }


def _method_from_dict(d: dict) -> Method:
    return Method(
        name=d["name"], descriptor=d["descriptor"], access=AccessFlag(d["access"]),
        arg_count=d["arg_count"], return_type=d["return_type"],
        xref_count=d["xref_count"], bytecode=b"", instr_count=d["instr_count"],
        opcode_xor=d["opcode_xor"], calls=tuple(d["calls"]),
        instantiates=tuple(d["instantiates"]),
        abstract=base64.b64decode(d["abstract"]),
    )


def _class_to_dict(c: Class) -> dict:
    sig = c.signature if c.signature is not None else compute_signature(c)
    return {
        "descriptor": c.descriptor, "package": c.package, "name": c.name,
        "source_file": c.source_file, "access": int(c.access),
        "is_inner": c.is_inner, "is_synthetic": c.is_synthetic,
        "is_external": c.is_external,
        "methods": [_method_to_dict(m) for m in c.methods],
        "fields": [{"name": f.name, "type_desc": f.type_desc, "access": int(f.access)}
                   for f in c.fields],
        "strings": list(c.strings), "superclass": c.superclass,
        "interfaces": list(c.interfaces),
        "signature": [sig.cls, sig.fld, sig.mth, sig.code],
    }


def _class_from_dict(d: dict) -> Class:
    s = d["signature"]
    return Class(
        descriptor=d["descriptor"], package=d["package"], name=d["name"],
        source_file=d["source_file"], access=AccessFlag(d["access"]),
        is_inner=d["is_inner"], is_synthetic=d["is_synthetic"],
        is_external=d["is_external"],
        methods=tuple(_method_from_dict(m) for m in d["methods"]),
        fields=tuple(Field(name=f["name"], type_desc=f["type_desc"],
                           access=AccessFlag(f["access"])) for f in d["fields"]),
        strings=tuple(d["strings"]), superclass=d["superclass"],
        interfaces=tuple(d["interfaces"]),
        signature=Signature(cls=s[0], fld=s[1], mth=s[2], code=s[3]),
    )


def _manifest_to_dict(m: ManifestInfo) -> dict:
    return {"package": m.package, "activities": list(m.activities),
            "services": list(m.services), "receivers": list(m.receivers),
            "providers": list(m.providers)}


def _manifest_from_dict(d: dict) -> ManifestInfo:
    return ManifestInfo(package=d["package"], activities=tuple(d["activities"]),
                        services=tuple(d["services"]), receivers=tuple(d["receivers"]),
                        providers=tuple(d["providers"]))


def record_to_dict(rec: PreparedRecord) -> dict:
    return {
        "record_version": rec.record_version, "algo_version": rec.algo_version,
        "digest": rec.digest, "input_kind": rec.input_kind,
        "normalized": rec.normalized,
        "manifest": _manifest_to_dict(rec.manifest) if rec.manifest else None,
        "classes": [_class_to_dict(c) for c in rec.classes],
    }


def record_from_dict(d: dict) -> PreparedRecord:
    return PreparedRecord(
        record_version=d["record_version"], algo_version=d["algo_version"],
        digest=d["digest"], input_kind=d["input_kind"], normalized=d["normalized"],
        manifest=_manifest_from_dict(d["manifest"]) if d["manifest"] else None,
        classes=tuple(_class_from_dict(c) for c in d["classes"]),
    )


def save(rec: PreparedRecord, path: str | Path) -> Path:
    p = Path(path)
    p.write_text(json.dumps(record_to_dict(rec)))
    return p


def load_record(path: str | Path) -> PreparedRecord:
    rec = record_from_dict(json.loads(Path(path).read_text()))
    if rec.algo_version != ALGO_VERSION:
        raise ValueError(
            f"record {rec.digest[:12]} was prepared with algo_version "
            f"{rec.algo_version!r} but this build is {ALGO_VERSION!r} — re-prepare it"
        )
    return rec
