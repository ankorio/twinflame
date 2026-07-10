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

Records are version-stamped **per layer**, because the three kinds of data in a
record have different lifetimes and different recovery costs:

- `extraction_version` — facts the loader pulls from the DEX (strings, calls,
  hierarchy, counts). Stale ⇒ only a true re-prepare from the sample helps.
- `abstract_version` — the abstract-opcode sequences (opcode category table).
  Stale ⇒ re-prepare (the raw bytecode the sequences derive from is dropped).
- `signature_version` — the SimHash parameters. Stale ⇒ **recomputable in
  place** from the stored abstract sequences via `migrate_record` — no sample,
  no parse. This is the common case as the algorithm is tuned, and the reason
  a large prepared DB survives most releases (`twinflame migrate`).

A record whose required layers differ from the running code's is rejected at
load — never silently compared. `ALGO_VERSION` (the combined stamp) remains the
cheap all-layers-current identity.

On disk a record is a packed binary `.tfr` file (magic `TFR` + version byte,
zlib-deflated payload): one interned string table for every repeated string
(descriptors, call targets, type names), LEB128 varints for all integers, raw
bytes for the abstract-opcode sequences, 16 fixed bytes per 128-bit signature.
Records are consumed only by twinflame itself, so the format trades human
readability for size — bulk-prepare storage is the constraint. The dict/JSON
codec (`record_to_dict`/`record_from_dict`) is kept for tests and debugging.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .loader import EXTRACTION_VERSION
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
# v2: packed binary .tfr format (was JSON).
RECORD_VERSION = 2

# On-disk magic: b"TFR" + the record-layout version byte.
MAGIC = b"TFR" + bytes([RECORD_VERSION])


def _stamp(*material) -> str:
    return hashlib.sha256(repr(material).encode()).hexdigest()[:16]


# Per-layer stamps (see module docstring for what invalidates each and how it
# recovers). Comparability of two records = equality of the layers a given
# operation consumes.
EXTRACTION_STAMP = _stamp("extraction", EXTRACTION_VERSION)
ABSTRACT_STAMP = _stamp("abstract", bytes(CATEGORY))
SIGNATURE_STAMP = _stamp(
    "signature", PARTIAL_BITS, SIGNATURE_BITS, DEFAULT_SEED,
    SUPER_WEIGHT, IFACE_WEIGHT, CALL_WEIGHT_MULT,
    N_PERMUTATIONS_DEFAULT, BUCKET_PREFIX_BITS_DEFAULT,
)

# Combined all-layers-current identity (layout is deliberately NOT folded in:
# old layouts stay readable through their versioned decoders).
ALGO_VERSION = _stamp("algo", EXTRACTION_STAMP, ABSTRACT_STAMP, SIGNATURE_STAMP)

# The layers migrate_record can rebuild from what a record already stores.
_RECOMPUTABLE_LAYERS = frozenset({"signature"})


def _legacy_v1_algo_version() -> str:
    """The monolithic stamp a v1 (JSON-era) record carries when its *content*
    was produced by exactly the extraction/abstract/signature code now running
    — the pre-layering formula, with the v1 layout number folded in. Matching
    it proves a legacy record is current in every layer and can be re-encoded
    as packed v2 without touching the sample."""
    material = repr((
        1, EXTRACTION_VERSION, PARTIAL_BITS, SIGNATURE_BITS, DEFAULT_SEED,
        SUPER_WEIGHT, IFACE_WEIGHT, CALL_WEIGHT_MULT,
        N_PERMUTATIONS_DEFAULT, BUCKET_PREFIX_BITS_DEFAULT,
    )).encode() + bytes(CATEGORY)
    return hashlib.sha256(material).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PreparedRecord:
    """Everything needed to compare a sample later, keyed by its digest."""

    record_version: int
    extraction_version: str
    abstract_version: str
    signature_version: str
    digest: str
    input_kind: str            # "apk" | "dex" | "dex-dir"
    normalized: bool
    manifest: Optional[ManifestInfo]
    classes: tuple[Class, ...]  # signature-populated, methods carry `abstract`, no bytecode

    @property
    def algo_version(self) -> str:
        """Combined identity of the record's layer stamps (== the module-level
        `ALGO_VERSION` iff every layer is current)."""
        return _stamp("algo", self.extraction_version, self.abstract_version,
                      self.signature_version)

    def stale_layers(self) -> tuple[str, ...]:
        """Names of layers whose stamp differs from the running code's."""
        return tuple(name for name, mine, current in (
            ("extraction", self.extraction_version, EXTRACTION_STAMP),
            ("abstract", self.abstract_version, ABSTRACT_STAMP),
            ("signature", self.signature_version, SIGNATURE_STAMP),
        ) if mine != current)

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
        extraction_version=EXTRACTION_STAMP,
        abstract_version=ABSTRACT_STAMP,
        signature_version=SIGNATURE_STAMP,
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
        "extraction_version": rec.extraction_version,
        "abstract_version": rec.abstract_version,
        "signature_version": rec.signature_version,
        "digest": rec.digest, "input_kind": rec.input_kind,
        "normalized": rec.normalized,
        "manifest": _manifest_to_dict(rec.manifest) if rec.manifest else None,
        "classes": [_class_to_dict(c) for c in rec.classes],
    }


def _layers_from_dict(d: dict) -> tuple[str, str, str]:
    """Layer stamps for a dict-form record. A legacy v1 record has only the
    monolithic `algo_version`: when it equals what v1 code with *today's*
    parameters would have stamped, every layer is provably current; otherwise
    something changed but v1 can't say what — mark all layers stale so the
    record is rejected (and reported non-migratable) rather than guessed at."""
    if "extraction_version" in d:
        return d["extraction_version"], d["abstract_version"], d["signature_version"]
    if d["algo_version"] == _legacy_v1_algo_version():
        return EXTRACTION_STAMP, ABSTRACT_STAMP, SIGNATURE_STAMP
    legacy = "legacy:" + d["algo_version"]
    return legacy, legacy, legacy


def record_from_dict(d: dict) -> PreparedRecord:
    extraction, abstract, signature = _layers_from_dict(d)
    return PreparedRecord(
        record_version=d["record_version"],
        extraction_version=extraction, abstract_version=abstract,
        signature_version=signature,
        digest=d["digest"], input_kind=d["input_kind"], normalized=d["normalized"],
        manifest=_manifest_from_dict(d["manifest"]) if d["manifest"] else None,
        classes=tuple(_class_from_dict(c) for c in d["classes"]),
    )


# --- packed binary codec (.tfr) ----------------------------------------------
#
# Payload layout (after MAGIC, whole payload zlib-deflated):
#   header:   extraction/abstract/signature layer stamps (3 × 8 raw bytes of
#             the 16-hex stamps) · digest (str) · input_kind (1 byte enum) ·
#             flags (bit0 normalized, bit1 manifest)
#   strings:  varint count, then varint-length-prefixed UTF-8 entries; every
#             string below is a varint index into this table. Optional strings
#             (source_file, superclass) encode 0 = None, else index+1.
#   manifest: package + the four component lists (if flags bit1)
#   classes:  varint count × class; methods nested per class; abstract-opcode
#             sequences as varint-length-prefixed raw bytes; signatures as
#             4 × uint32 big-endian.

_INPUT_KINDS = ("apk", "dex", "dex-dir")


def _w_varint(out: bytearray, v: int) -> None:
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def _r_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


class _StringTable:
    """Interns every string once; emits varint indices."""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}
        self.entries: list[str] = []

    def add(self, s: str) -> int:
        idx = self._index.get(s)
        if idx is None:
            idx = self._index[s] = len(self.entries)
            self.entries.append(s)
        return idx


def _w_str_seq(out: bytearray, tab: _StringTable, seq) -> None:
    _w_varint(out, len(seq))
    for s in seq:
        _w_varint(out, tab.add(s))


def _r_str_seq(buf: bytes, pos: int, tab: list[str]) -> tuple[tuple[str, ...], int]:
    n, pos = _r_varint(buf, pos)
    out = []
    for _ in range(n):
        i, pos = _r_varint(buf, pos)
        out.append(tab[i])
    return tuple(out), pos


def record_to_bytes(rec: PreparedRecord) -> bytes:
    tab = _StringTable()
    body = bytearray()

    # Two passes would let the table precede the body; instead the body refers
    # to the table by index and the table is written *first* at assembly time,
    # so a single pass suffices: build body while interning, then prepend.
    def w_opt(s: Optional[str]) -> None:
        _w_varint(body, 0 if s is None else tab.add(s) + 1)

    if rec.manifest is not None:
        _w_varint(body, tab.add(rec.manifest.package))
        for seq in (rec.manifest.activities, rec.manifest.services,
                    rec.manifest.receivers, rec.manifest.providers):
            _w_str_seq(body, tab, seq)

    _w_varint(body, len(rec.classes))
    for c in rec.classes:
        sig = c.signature if c.signature is not None else compute_signature(c)
        _w_varint(body, tab.add(c.descriptor))
        _w_varint(body, tab.add(c.package))
        _w_varint(body, tab.add(c.name))
        w_opt(c.source_file)
        w_opt(c.superclass)
        _w_varint(body, int(c.access))
        body.append(c.is_inner | c.is_synthetic << 1 | c.is_external << 2)
        for part in (sig.cls, sig.fld, sig.mth, sig.code):
            body += part.to_bytes(4, "big")
        _w_str_seq(body, tab, c.interfaces)
        _w_str_seq(body, tab, c.strings)
        _w_varint(body, len(c.fields))
        for f in c.fields:
            _w_varint(body, tab.add(f.name))
            _w_varint(body, tab.add(f.type_desc))
            _w_varint(body, int(f.access))
        _w_varint(body, len(c.methods))
        for m in c.methods:
            _w_varint(body, tab.add(m.name))
            _w_varint(body, tab.add(m.descriptor))
            _w_varint(body, int(m.access))
            _w_varint(body, m.arg_count)
            _w_varint(body, tab.add(m.return_type))
            _w_varint(body, m.xref_count)
            _w_varint(body, m.instr_count)
            _w_varint(body, m.opcode_xor)
            _w_str_seq(body, tab, m.calls)
            _w_str_seq(body, tab, m.instantiates)
            abstract = m.abstract if m.abstract is not None else categorize(m.bytecode)
            _w_varint(body, len(abstract))
            body += abstract

    payload = bytearray()
    for stamp in (rec.extraction_version, rec.abstract_version, rec.signature_version):
        payload += bytes.fromhex(stamp)
    digest = rec.digest.encode()
    _w_varint(payload, len(digest))
    payload += digest
    payload.append(_INPUT_KINDS.index(rec.input_kind))
    payload.append(rec.normalized | (rec.manifest is not None) << 1)
    _w_varint(payload, len(tab.entries))
    for s in tab.entries:
        # DEX strings are MUTF-8: unpaired surrogates are legal and do occur
        # in real APKs, so strict UTF-8 would refuse real class strings.
        e = s.encode("utf-8", "surrogatepass")
        _w_varint(payload, len(e))
        payload += e
    payload += body
    return MAGIC + zlib.compress(bytes(payload), 9)


def record_from_bytes(data: bytes) -> PreparedRecord:
    if data[:3] != MAGIC[:3]:
        raise ValueError("not a twinflame record (bad magic)")
    if data[3] != RECORD_VERSION:
        raise ValueError(
            f"record layout version {data[3]} unsupported "
            f"(this build reads v{RECORD_VERSION}) — re-prepare it"
        )
    buf = zlib.decompress(data[4:])

    extraction_version = buf[:8].hex()
    abstract_version = buf[8:16].hex()
    signature_version = buf[16:24].hex()
    n, pos = _r_varint(buf, 24)
    digest = buf[pos:pos + n].decode()
    pos += n
    input_kind = _INPUT_KINDS[buf[pos]]
    flags = buf[pos + 1]
    pos += 2

    n, pos = _r_varint(buf, pos)
    tab: list[str] = []
    for _ in range(n):
        ln, pos = _r_varint(buf, pos)
        tab.append(buf[pos:pos + ln].decode("utf-8", "surrogatepass"))
        pos += ln

    def r_opt(pos: int) -> tuple[Optional[str], int]:
        i, pos = _r_varint(buf, pos)
        return (None if i == 0 else tab[i - 1]), pos

    manifest = None
    if flags & 2:
        i, pos = _r_varint(buf, pos)
        seqs = []
        for _ in range(4):
            seq, pos = _r_str_seq(buf, pos, tab)
            seqs.append(seq)
        manifest = ManifestInfo(package=tab[i], activities=seqs[0],
                                services=seqs[1], receivers=seqs[2], providers=seqs[3])

    n_classes, pos = _r_varint(buf, pos)
    classes = []
    for _ in range(n_classes):
        di, pos = _r_varint(buf, pos)
        pi, pos = _r_varint(buf, pos)
        ni, pos = _r_varint(buf, pos)
        source_file, pos = r_opt(pos)
        superclass, pos = r_opt(pos)
        access, pos = _r_varint(buf, pos)
        cflags = buf[pos]
        pos += 1
        sig_parts = [int.from_bytes(buf[pos + i * 4:pos + i * 4 + 4], "big") for i in range(4)]
        pos += 16
        interfaces, pos = _r_str_seq(buf, pos, tab)
        strings, pos = _r_str_seq(buf, pos, tab)
        n_fields, pos = _r_varint(buf, pos)
        fields = []
        for _ in range(n_fields):
            fn, pos = _r_varint(buf, pos)
            ft, pos = _r_varint(buf, pos)
            fa, pos = _r_varint(buf, pos)
            fields.append(Field(name=tab[fn], type_desc=tab[ft], access=AccessFlag(fa)))
        n_methods, pos = _r_varint(buf, pos)
        methods = []
        for _ in range(n_methods):
            mn, pos = _r_varint(buf, pos)
            md, pos = _r_varint(buf, pos)
            ma, pos = _r_varint(buf, pos)
            arg_count, pos = _r_varint(buf, pos)
            rt, pos = _r_varint(buf, pos)
            xref_count, pos = _r_varint(buf, pos)
            instr_count, pos = _r_varint(buf, pos)
            opcode_xor, pos = _r_varint(buf, pos)
            calls, pos = _r_str_seq(buf, pos, tab)
            instantiates, pos = _r_str_seq(buf, pos, tab)
            ln, pos = _r_varint(buf, pos)
            abstract = buf[pos:pos + ln]
            pos += ln
            methods.append(Method(
                name=tab[mn], descriptor=tab[md], access=AccessFlag(ma),
                arg_count=arg_count, return_type=tab[rt], xref_count=xref_count,
                bytecode=b"", instr_count=instr_count, opcode_xor=opcode_xor,
                calls=calls, instantiates=instantiates, abstract=abstract,
            ))
        classes.append(Class(
            descriptor=tab[di], package=tab[pi], name=tab[ni],
            source_file=source_file, access=AccessFlag(access),
            is_inner=bool(cflags & 1), is_synthetic=bool(cflags & 2),
            is_external=bool(cflags & 4),
            methods=tuple(methods), fields=tuple(fields), strings=strings,
            superclass=superclass, interfaces=interfaces,
            signature=Signature(cls=sig_parts[0], fld=sig_parts[1],
                                mth=sig_parts[2], code=sig_parts[3]),
        ))

    return PreparedRecord(
        record_version=RECORD_VERSION,
        extraction_version=extraction_version, abstract_version=abstract_version,
        signature_version=signature_version, digest=digest,
        input_kind=input_kind, normalized=bool(flags & 1), manifest=manifest,
        classes=tuple(classes),
    )


def save(rec: PreparedRecord, path: str | Path) -> Path:
    p = Path(path)
    # Create missing parent directories so a record path into a not-yet-existing
    # folder writes cleanly instead of crashing.
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(record_to_bytes(rec))
    return p


def is_record_file(path: str | Path) -> bool:
    """True if `path` is a packed `.tfr` record (any layout version) or a
    legacy JSON record — i.e. something `load_record` should be handed."""
    p = Path(path)
    if not p.is_file():
        return False
    with p.open("rb") as fh:
        head = fh.read(4)
    return head[:3] == MAGIC[:3] or (head[:1] == b"{" and p.name.endswith(".tfr.json"))


def load_record(path: str | Path, *, check: bool = True) -> PreparedRecord:
    """Load a record (packed v2 or legacy v1 JSON). With `check` (the default)
    a record with any stale layer is rejected, with the recovery path in the
    message; `check=False` is for `migrate_record`, which needs the stale
    record in hand to fix it."""
    data = Path(path).read_bytes()
    if data[:1] == b"{":  # legacy v1 JSON record
        rec = record_from_dict(json.loads(data))
    else:
        rec = record_from_bytes(data)
    if check:
        stale = rec.stale_layers()
        if stale:
            fix = ("`twinflame migrate` can refresh it in place"
                   if set(stale) <= _RECOMPUTABLE_LAYERS
                   else "re-prepare it from the sample")
            raise ValueError(
                f"record {rec.digest[:12]} is stale "
                f"({', '.join(stale)} layer{'s' if len(stale) > 1 else ''} changed) — {fix}"
            )
    return rec


def migrate_record(rec: PreparedRecord) -> Optional[PreparedRecord]:
    """Bring a record up to the running code's layer stamps without touching
    the original sample, when possible.

    Returns the record itself if already current, an upgraded copy if every
    stale layer is recomputable from what the record stores (today: the
    signature layer, rebuilt from the abstract sequences), or `None` when only
    a true re-prepare helps (extraction/abstract changes — their inputs are
    not in the record)."""
    stale = set(rec.stale_layers())
    if not stale:
        # Content current; normalize the layout stamp so a legacy record
        # re-saves as packed v2.
        return rec if rec.record_version == RECORD_VERSION \
            else replace(rec, record_version=RECORD_VERSION)
    if stale - _RECOMPUTABLE_LAYERS:
        return None
    classes = tuple(
        replace(c, signature=compute_signature(replace(c, signature=None)))
        for c in rec.classes
    )
    return replace(rec, record_version=RECORD_VERSION,
                   signature_version=SIGNATURE_STAMP, classes=classes)
