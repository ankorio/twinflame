"""`twinflame serve` — a long-lived request loop for editor and decompiler plugins.

One process per host session (e.g. one jadx-gui window). Requests arrive as
NDJSON on stdin, one JSON object per line; every request gets exactly one
response line on stdout, optionally preceded by progress events. Loaded
inputs stay in memory, so the expensive part — parsing DEX and computing
signatures — happens once per input, and every later `describe` or
`pack.match` is a dictionary lookup plus a Hamming query.

Wire format (protocol version 1):

    -> {"v": 1, "id": <any>, "op": "<name>", "params": {...}}
    <- {"id": <same>, "ok": true,  "result": {...}}
    <- {"id": <same>, "ok": false, "error": {"code": "<slug>", "message": "..."}}
    <- {"id": <same>, "event": "progress", "data": {...}}     (0..n, before the response)

Ops: `hello`, `load`, `status`, `where`, `describe`, `pack.build`,
`pack.match`, `pack.nearest`, `pack.list`, `shutdown`. See the handlers below for parameters
and results; `Session` and `dispatch` are usable in-process (tests, other
Python hosts) without the stdin/stdout loop.

Inputs are addressed as `(input_index, descriptor)`, where `input_index` is
the caller's own index for the file (jadx: position in its input list). Each
input file is loaded on its own — never merged with its siblings — so a
descriptor present in an APK *and* in a dumped `.dex` yields two distinct
classes, and a hit reports which file it came from.

Signatures cross the wire as 32-character lower-case hex (the combined
128-bit value, `Signature.combined`).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .model import App, Class
from .signature import compute_signature

PROTOCOL_VERSION = 1

# `twinflame.prepare` the *module* is shadowed by the `prepare()` function the
# package re-exports; fetch the module explicitly.
_prepare = importlib.import_module("twinflame.prepare")

_LIBSIGS_HINT = (
    "pack operations require the twinflame_libsigs package "
    "(pip install twinflame_libsigs)"
)

# `pack.nearest` default: wide enough to show a class that is *almost* an
# entry (a quarter of the 128 signature bits) without listing everything.
NEAREST_RADIUS = 32


class ServeError(Exception):
    """A request-level failure: reported to the caller, never fatal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- inputs -------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LoadedInput:
    index: int
    path: Path
    kind: str                  # "apk" | "dex" | "dex-dir" | "tfr"
    digest: str
    record: Any                # PreparedRecord: classes carry signature + abstract
    by_desc: Dict[str, Class]
    seconds: float
    from_cache: bool

    @property
    def classes(self) -> Sequence[Class]:
        return self.record.classes


def detect_kind(path: Path) -> str:
    """What a file is, by content first (a dump named `payload.bin` is still a
    DEX), extension second. Raises `ServeError("unknown-kind")`."""
    if path.is_dir():
        return "dex-dir"
    if not path.is_file():
        raise ServeError("not-found", f"file not found: {path}")
    with path.open("rb") as fh:
        head = fh.read(8)
    if head[:3] == b"dex":
        return "dex"
    if head[:3] == _prepare.MAGIC[:3]:
        return "tfr"
    if head[:1] == b"{" and path.name.endswith(".tfr.json"):
        return "tfr"
    if head[:2] == b"PK":
        return "apk"
    suffix = path.suffix.lower()
    if suffix == ".dex":
        return "dex"
    if suffix in (".apk", ".zip", ".jar", ".apks", ".xapk"):
        return "apk"
    raise ServeError("unknown-kind", f"cannot tell what {path.name} is (not DEX, ZIP or .tfr)")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_input(
    index: int,
    path: str | Path,
    *,
    digest: Optional[str] = None,
    cache_dir: Optional[Path] = None,
) -> LoadedInput:
    """Load one file into a prepared (signature-populated) view. With
    `cache_dir`, a `<digest>.tfr` record is reused when present and current,
    and written after a fresh parse. A stale cached record is replaced."""
    from .loader import LoadError, load, load_dex

    p = Path(path)
    t0 = time.perf_counter()
    kind = detect_kind(p)
    from_cache = False
    rec = None
    if kind == "tfr":
        try:
            rec = _prepare.load_record(p)
        except ValueError as e:
            raise ServeError("stale-record", str(e)) from e
        digest = rec.digest
    else:
        if digest is None:
            digest = _prepare.digest_of(p) if p.is_dir() else _sha256_file(p)
        cached = (cache_dir / f"{digest}.tfr") if cache_dir else None
        if cached is not None and cached.is_file():
            try:
                rec = _prepare.load_record(cached)
                from_cache = True
            except (ValueError, OSError):
                rec = None  # stale or corrupt cache entry: rebuild below
        if rec is None:
            try:
                if kind in ("dex", "dex-dir"):
                    app = load_dex([p])
                else:
                    app = load(p)
            except LoadError as e:
                raise ServeError("load-failed", str(e)) from e
            rec = _prepare.prepare_app(app, digest=digest, input_kind=kind)
            if cached is not None:
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    _prepare.save(rec, cached)
                except OSError:
                    pass  # a cache is a convenience, never a failure
    by_desc = {c.descriptor: c for c in rec.classes}
    return LoadedInput(
        index=index, path=p, kind=kind, digest=digest, record=rec,
        by_desc=by_desc, seconds=time.perf_counter() - t0, from_cache=from_cache,
    )


# --- session ------------------------------------------------------------------

def _sig_hex(c: Class) -> str:
    return f"{compute_signature(c).combined:032x}"


def _fqcn(c: Class) -> str:
    return f"{c.package}.{c.name}" if c.package else c.name


def _anchor_strings(c: Class) -> List[str]:
    try:
        from twinflame_libsigs.strings import useful_strings
    except ImportError:
        return []
    from .family import MAX_ENTRY_STRINGS
    return sorted(useful_strings(c.strings), key=lambda s: (-len(s), s))[:MAX_ENTRY_STRINGS]


def _read_pack_spec(spec: dict) -> tuple[list, dict, dict, list]:
    """Entries, payloads and meta of one pack spec (`path`, `extra_entries?`)
    as `pack.match` / `pack.nearest` take it, the extra entries appended with
    ids continuing after the pack's own — the append-only feedback loop.
    Raises `ServeError("stale-stamp" | "unreadable")` for a pack that cannot
    be used; extra entries without a valid hex signature are dropped and their
    would-be ids returned as the fourth element."""
    from twinflame_libsigs.pack import read_meta, read_pack
    from twinflame_libsigs.store import StaleStoreError

    path = Path(spec.get("path", ""))
    try:
        entries, payloads, _ = read_pack(path, expect_stamp=_prepare.SIGNATURE_STAMP)
    except StaleStoreError as e:
        raise ServeError("stale-stamp", str(e)) from e
    except (OSError, ValueError) as e:
        raise ServeError("unreadable", str(e)) from e
    meta = read_meta(path)
    entries = list(entries)
    payloads = dict(payloads)
    bad_extra: list = []
    for extra in spec.get("extra_entries") or []:
        pid = len(entries)
        try:
            sig = int(extra["signature"], 16)
        except (KeyError, TypeError, ValueError):
            bad_extra.append(pid)
            continue
        entries.append((pid, sig, tuple(extra.get("strings") or ())))
        payloads[str(pid)] = {
            "fqcn": extra.get("fqcn", f"extra/{pid}"),
            "coverage": 1, "samples": [], "max_dist": 0,
            "instructions": int(extra.get("instructions", 0)),
            **{k: v for k, v in extra.items() if k not in ("signature", "strings")},
            "extra": True,
        }
    return entries, payloads, meta, bad_extra


class Session:
    """State behind one `serve` process: the loaded inputs plus lazily built
    helpers (the library dictionary detector)."""

    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        libsigs: Optional[str] = None,
        use_libsigs: bool = True,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.libsigs_path = libsigs
        self.use_libsigs = use_libsigs
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.inputs: Dict[int, LoadedInput] = {}
        self._libsigs_state: Optional[tuple] = None   # (pack path | None, detector | None)

    # -- helpers ---------------------------------------------------------------

    def _libsigs(self):
        if self._libsigs_state is None:
            from . import libdict
            pack = libdict.find_pack(self.libsigs_path) if self.use_libsigs else None
            det = libdict.load_detector(pack, log=self.log) if pack else None
            self._libsigs_state = (pack, det)
        return self._libsigs_state

    def _input(self, index: Any) -> LoadedInput:
        try:
            inp = self.inputs[int(index)]
        except (KeyError, TypeError, ValueError):
            raise ServeError("no-such-input", f"input {index!r} is not loaded")
        return inp

    def _class(self, index: Any, descriptor: Any) -> tuple[LoadedInput, Class]:
        inp = self._input(index)
        c = inp.by_desc.get(descriptor)
        if c is None:
            raise ServeError("no-such-class", f"{descriptor!r} is not in input {inp.index} ({inp.path.name})")
        return inp, c

    def _holders(self, descriptor: str) -> List[int]:
        return sorted(i for i, inp in self.inputs.items() if descriptor in inp.by_desc)

    # -- ops -------------------------------------------------------------------

    def op_hello(self, params: dict) -> dict:
        from . import __version__
        from .signature import _native as native_sig
        try:
            import twinflame_libsigs
            from twinflame_libsigs.detect import _HAVE_NATIVE as native_mih
            libsigs_version = getattr(twinflame_libsigs, "__version__", "?")
        except ImportError:
            native_mih, libsigs_version = False, None
        pack, _ = self._libsigs() if libsigs_version else (None, None)
        return {
            "protocol": PROTOCOL_VERSION,
            "twinflame": __version__,
            "twinflame_libsigs": libsigs_version,
            "stamp": _prepare.SIGNATURE_STAMP,
            "algo": _prepare.ALGO_VERSION,
            "native_signature": native_sig is not None,
            "native_mih": bool(native_mih),
            "libsigs_pack": str(pack) if pack else None,
            "python": sys.version.split()[0],
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
        }

    def op_load(self, params: dict, emit=None) -> dict:
        """`inputs: [{index, path, digest?}]`. Replaces the current input set.
        A failing input is reported in its own row; the others still load."""
        specs = params.get("inputs")
        if not isinstance(specs, list) or not specs:
            raise ServeError("bad-params", "load needs a non-empty 'inputs' list")
        self.inputs = {}
        rows = []
        for spec in specs:
            try:
                index = int(spec["index"])
                path = spec["path"]
            except (KeyError, TypeError, ValueError):
                raise ServeError("bad-params", "each input needs an integer 'index' and a 'path'")
            if index in self.inputs:
                raise ServeError("bad-params", f"duplicate input index {index}")
            try:
                inp = load_input(index, path, digest=spec.get("digest"), cache_dir=self.cache_dir)
            except ServeError as e:
                rows.append({"index": index, "path": str(path), "error": {"code": e.code, "message": e.message}})
                continue
            self.inputs[index] = inp
            rows.append({
                "index": index, "path": str(inp.path), "kind": inp.kind, "digest": inp.digest,
                "classes": len(inp.classes), "seconds": round(inp.seconds, 3),
                "from_cache": inp.from_cache,
            })
            if emit:
                emit({"loaded": index, "classes": len(inp.classes)})
        return {"inputs": rows, "stamp": _prepare.SIGNATURE_STAMP}

    def op_status(self, params: dict) -> dict:
        return {"inputs": [
            {"index": i, "path": str(inp.path), "kind": inp.kind, "digest": inp.digest,
             "classes": len(inp.classes)}
            for i, inp in sorted(self.inputs.items())
        ]}

    def op_where(self, params: dict) -> dict:
        """`descriptors: [...]` -> which loaded inputs hold each one."""
        descs = params.get("descriptors")
        if not isinstance(descs, list):
            raise ServeError("bad-params", "where needs a 'descriptors' list")
        return {"where": {d: self._holders(d) for d in descs}}

    def op_describe(self, params: dict) -> dict:
        from . import family
        from .boilerplate import is_boilerplate
        inp, c = self._class(params.get("input_index"), params.get("descriptor"))
        sig = compute_signature(c)
        libsigs = None
        _, det = self._libsigs()
        if det is not None:
            d = det.detect(sig.combined, c.strings)
            if d is not None:
                meta = det.resolve(d) or {}
                libsigs = {
                    "label": det.label(d), "coord": meta.get("coord"),
                    "ranges": list(meta.get("ranges", ())), "fqcn": meta.get("fqcn"),
                    "tier": d.tier, "distance": d.distance,
                }
        return {
            "input_index": inp.index, "input": str(inp.path), "digest": inp.digest,
            "descriptor": c.descriptor, "fqcn": _fqcn(c),
            "signature": f"{sig.combined:032x}",
            "instructions": c.total_instructions,
            "methods": len(c.methods), "fields": len(c.fields), "strings": len(c.strings),
            "superclass": c.superclass, "interfaces": list(c.interfaces),
            "boilerplate": is_boilerplate(c),
            "below_min_instructions": c.total_instructions < family.DEFAULT_MIN_INSTRUCTIONS,
            "min_instructions": family.DEFAULT_MIN_INSTRUCTIONS,
            "libsigs": libsigs,
            "duplicates": [i for i in self._holders(c.descriptor) if i != inp.index],
            # the tier-3 anchors a pack entry for this class would carry (same
            # selection as build_curated_pack), for append-only addition records
            "anchor_strings": _anchor_strings(c),
            "method_ids": [f"{m.name}{m.descriptor}" for m in c.methods],
        }

    def op_pack_build(self, params: dict) -> dict:
        """`entries: [{input_index, descriptor, note?, methods?}]`, `name`, `out_dir`,
        `meta?` (merged into the pack meta), `filename?` (default pack.tflp),
        `radius?`, `min_instructions?`. Writes the pack + sidecar; returns
        their hashes so the caller can seal a manifest."""
        try:
            from . import family
        except ImportError as e:
            raise ServeError("no-libsigs", _LIBSIGS_HINT) from e
        specs = params.get("entries")
        name = params.get("name")
        out_dir = params.get("out_dir")
        if not isinstance(specs, list) or not specs:
            raise ServeError("bad-params", "pack.build needs a non-empty 'entries' list")
        if not name or not out_dir:
            raise ServeError("bad-params", "pack.build needs 'name' and 'out_dir'")
        classes: List[Class] = []
        sample_ids: List[int] = []
        samples: List[tuple[str, str]] = []
        sample_of_input: Dict[int, int] = {}
        payload_extra: Dict[int, dict] = {}
        for i, spec in enumerate(specs):
            inp, c = self._class(spec.get("input_index"), spec.get("descriptor"))
            if inp.index not in sample_of_input:
                sample_of_input[inp.index] = len(samples)
                samples.append((inp.path.name, inp.digest))
            classes.append(c)
            sample_ids.append(sample_of_input[inp.index])
            payload_extra[i] = {
                "descriptor": c.descriptor,
                "input_index": inp.index, "input": inp.path.name, "digest": inp.digest,
                "note": spec.get("note") or "",
            }
            methods = spec.get("methods")
            if methods:
                # analyst-selected methods of the class, kept for the method-level
                # matcher (v2); the class signature is still what v1 matches on
                known = {f"{m.name}{m.descriptor}" for m in c.methods}
                payload_extra[i]["methods"] = [m for m in methods if m in known]
        kwargs: Dict[str, Any] = {}
        if params.get("radius") is not None:
            kwargs["radius"] = int(params["radius"])
        if params.get("min_instructions") is not None:
            kwargs["min_instructions"] = int(params["min_instructions"])
        try:
            fam = family.build_curated_pack(
                classes, name=str(name), sample_ids=sample_ids, samples=samples, **kwargs)
        except ImportError as e:
            raise ServeError("no-libsigs", str(e)) from e
        except ValueError as e:
            raise ServeError("bad-params", str(e)) from e
        extra_meta = dict(params.get("meta") or {})
        extra_meta.setdefault("curated", True)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        pack_path = out / str(params.get("filename") or "pack.tflp")
        try:
            family.save_family(fam, pack_path, extra_meta=extra_meta, payload_extra=payload_extra)
        except ImportError as e:
            raise ServeError("no-libsigs", str(e)) from e
        from twinflame_libsigs.pack import sidecar_path
        sc = sidecar_path(pack_path)
        return {
            "path": str(pack_path), "sidecar": str(sc),
            "stamp": _prepare.SIGNATURE_STAMP, "n_entries": len(fam.entries),
            "radius_build": fam.radius_build,
            "sha256": {"pack": _sha256_file(pack_path), "sidecar": _sha256_file(sc)},
            "entries": [
                {"id": i, "descriptor": classes[i].descriptor, "fqcn": e.fqcn,
                 "instructions": e.instructions, "strings": len(e.strings),
                 "signature": f"{e.signature:032x}"}
                for i, e in enumerate(fam.entries)
            ],
        }

    def op_pack_match(self, params: dict, emit=None) -> dict:
        """`packs: [{id?, path, radius?, min_instructions?, extra_entries?}]`,
        `inputs?: [index...]` (default all), `use_strings?` (default true).
        One result row per (pack, input). Packs that cannot be used are
        listed under `skipped` with a reason instead of failing the call.
        `extra_entries` (`[{signature, strings?, fqcn?, instructions?, ...}]`)
        are appended to the pack's own entries with ids continuing after
        them — the append-only feedback loop."""
        try:
            from . import family
            from twinflame_libsigs.detect import LibraryDetector
            from twinflame_libsigs.pack import read_meta, read_pack
            from twinflame_libsigs.store import StaleStoreError
        except ImportError as e:
            raise ServeError("no-libsigs", _LIBSIGS_HINT) from e
        from .score import DEFAULT_MATCH_RADIUS

        specs = params.get("packs")
        if not isinstance(specs, list) or not specs:
            raise ServeError("bad-params", "pack.match needs a non-empty 'packs' list")
        indexes = params.get("inputs")
        if indexes is None:
            targets = [self.inputs[i] for i in sorted(self.inputs)]
        else:
            targets = [self._input(i) for i in indexes]
        if not targets:
            raise ServeError("no-inputs", "nothing is loaded")
        use_strings = bool(params.get("use_strings", True))

        results = []
        skipped = []
        for n, spec in enumerate(specs):
            path = Path(spec.get("path", ""))
            pack_id = spec.get("id") or path.stem
            try:
                entries, payloads, meta, bad_extra = _read_pack_spec(spec)
            except ServeError as e:
                skipped.append({"id": pack_id, "path": str(path), "reason": e.code, "message": e.message})
                continue
            for pid in bad_extra:
                skipped.append({"id": pack_id, "path": str(path), "reason": "bad-extra-entry",
                                "message": f"extra entry {pid} has no valid hex 'signature'"})
            radius = spec.get("radius")
            if radius is None:
                radius = meta.get("radius_build", DEFAULT_MATCH_RADIUS)
            radius = int(radius)
            min_instr = spec.get("min_instructions")
            if min_instr is None:
                min_instr = meta.get("min_instr", family.DEFAULT_MIN_INSTRUCTIONS)
            detector = LibraryDetector.build(entries, radius=radius)
            entry_strings = {pid: strs for pid, _sig, strs in entries}
            for inp in targets:
                r = family.match_family(
                    detector, meta, payloads, inp.classes, pack=str(path),
                    use_strings=use_strings, probe_min_instructions=int(min_instr),
                    entry_strings=entry_strings)
                row = {
                    "pack_id": pack_id, "path": str(path), "input_index": inp.index,
                    "input": str(inp.path), "family": r.family,
                    "score": round(r.score, 6), "present": r.present, "total": r.total,
                    "weight_present": r.weight_present, "weight_total": r.weight_total,
                    "by_tier": {str(k): v for k, v in sorted(r.by_tier.items())},
                    "radius": radius, "min_instructions": int(min_instr),
                    "hits": [
                        {"entry_id": pid, "entry_fqcn": fqcn, "descriptor": desc,
                         "tier": tier, "distance": dist,
                         # evidence: 1.0 for a signature hit, summed IDF of the
                         # shared strings for a tier-3 hit; plus those strings
                         "score": round(r.hit_scores.get(pid, 1.0), 4),
                         "strings": list(r.hit_strings.get(pid, ())),
                         "entry_instructions": payloads.get(str(pid), {}).get("instructions", 0),
                         "note": payloads.get(str(pid), {}).get("note", ""),
                         "extra": bool(payloads.get(str(pid), {}).get("extra", False))}
                        for pid, fqcn, desc, tier, dist in r.hits
                    ],
                }
                results.append(row)
                if emit:
                    emit({"pack": n + 1, "packs": len(specs), "pack_id": pack_id,
                          "input_index": inp.index, "score": row["score"], "present": r.present,
                          "total": r.total})
        return {"results": results, "skipped": skipped, "stamp": _prepare.SIGNATURE_STAMP}

    def op_pack_nearest(self, params: dict) -> dict:
        """`path`, `input_index`, `descriptors: [...]`, `radius?` (default
        `NEAREST_RADIUS`), `extra_entries?` (as in `pack.match`),
        `use_strings?` (default true). For each class its closest pack entry
        within the radius — the probe `pack.match` runs, at a radius wide
        enough to show classes that are *nearly* an entry but fall outside
        the pack's own radius (the neighbourhood of a hit). A class the input
        does not hold (`missing: true`) or with no entry within the radius
        gets `entry_id: null`."""
        try:
            from . import family
            from twinflame_libsigs.detect import LibraryDetector
        except ImportError as e:
            raise ServeError("no-libsigs", _LIBSIGS_HINT) from e
        descriptors = params.get("descriptors")
        if not isinstance(descriptors, list):
            raise ServeError("bad-params", "pack.nearest needs a 'descriptors' list")
        inp = self._input(params.get("input_index"))
        path = Path(params.get("path", ""))
        entries, payloads, _meta, _bad = _read_pack_spec(params)
        radius = int(params.get("radius", NEAREST_RADIUS))
        use_strings = bool(params.get("use_strings", True))
        detector = LibraryDetector.build(entries, radius=radius)
        entry_strings = {pid: strs for pid, _sig, strs in entries}
        results = []
        for desc in descriptors:
            c = inp.by_desc.get(desc)
            row: Dict[str, Any] = {"descriptor": desc, "entry_id": None, "missing": c is None}
            if c is not None:
                row["instructions"] = c.total_instructions
                sig = c.signature or compute_signature(c)
                hit = detector.detect(sig.combined, c.strings)
                if hit is not None and (use_strings or hit.tier != 3):
                    p = payloads.get(str(hit.payload_id), {})
                    row.update({
                        "entry_id": hit.payload_id, "entry_fqcn": p.get("fqcn", f"payload/{hit.payload_id}"),
                        "tier": hit.tier, "distance": hit.distance, "score": round(hit.score, 4),
                        "strings": list(family.shared_strings(c.strings, entry_strings.get(hit.payload_id, ()))),
                        "entry_instructions": p.get("instructions", 0), "note": p.get("note", ""),
                        "extra": bool(p.get("extra", False)),
                    })
            results.append(row)
        return {"pack_id": params.get("id") or path.stem, "path": str(path), "radius": radius,
                "results": results, "stamp": _prepare.SIGNATURE_STAMP}

    def op_pack_list(self, params: dict) -> dict:
        """`dir`, `recursive?` (default true): every `.tflp` under it with its
        sidecar meta and whether its stamp matches the running code. Reads
        only the sidecars, so it is cheap enough for a catalogue refresh."""
        d = params.get("dir")
        if not d:
            raise ServeError("bad-params", "pack.list needs 'dir'")
        root = Path(d)
        if not root.is_dir():
            raise ServeError("not-found", f"not a directory: {root}")
        try:
            from twinflame_libsigs.pack import sidecar_path
        except ImportError as e:
            raise ServeError("no-libsigs", _LIBSIGS_HINT) from e
        pattern = "**/*.tflp" if params.get("recursive", True) else "*.tflp"
        rows = []
        for pk in sorted(root.glob(pattern)):
            row: Dict[str, Any] = {"path": str(pk), "sidecar_ok": False, "stamp": None,
                                   "stamp_ok": False, "n_entries": None, "meta": {}}
            sc = sidecar_path(pk)
            try:
                doc = json.loads(sc.read_text())
                row.update({
                    "sidecar_ok": True, "stamp": doc.get("sig_stamp"),
                    "stamp_ok": doc.get("sig_stamp") == _prepare.SIGNATURE_STAMP,
                    "n_entries": doc.get("n_entries"), "meta": doc.get("meta") or {},
                })
            except (OSError, ValueError):
                pass
            rows.append(row)
        return {"packs": rows, "stamp": _prepare.SIGNATURE_STAMP}

    def op_shutdown(self, params: dict) -> dict:
        return {"bye": True}


_OPS: Dict[str, str] = {
    "hello": "op_hello",
    "load": "op_load",
    "status": "op_status",
    "where": "op_where",
    "describe": "op_describe",
    "pack.build": "op_pack_build",
    "pack.match": "op_pack_match",
    "pack.nearest": "op_pack_nearest",
    "pack.list": "op_pack_list",
    "shutdown": "op_shutdown",
}

_STREAMING = {"load", "pack.match"}


def dispatch(session: Session, request: Any, emit: Optional[Callable[[dict], None]] = None) -> dict:
    """Run one request and return its response object. `emit` receives
    progress events (already wrapped with the request id)."""
    rid = request.get("id") if isinstance(request, dict) else None

    def fail(code: str, message: str) -> dict:
        return {"id": rid, "ok": False, "error": {"code": code, "message": message}}

    if not isinstance(request, dict):
        return fail("bad-request", "request must be a JSON object")
    v = request.get("v", PROTOCOL_VERSION)
    if v != PROTOCOL_VERSION:
        return fail("bad-version", f"protocol {v!r} not supported (server speaks {PROTOCOL_VERSION})")
    op = request.get("op")
    handler_name = _OPS.get(op) if isinstance(op, str) else None
    if handler_name is None:
        return fail("unknown-op", f"unknown op {op!r}; known: {', '.join(sorted(_OPS))}")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return fail("bad-params", "'params' must be an object")
    handler = getattr(session, handler_name)
    try:
        if op in _STREAMING:
            wrapped = (lambda data: emit({"id": rid, "event": "progress", "data": data})) if emit else None
            result = handler(params, emit=wrapped)
        else:
            result = handler(params)
    except ServeError as e:
        return fail(e.code, e.message)
    except Exception as e:  # never let one request kill the session
        session.log(f"serve: {op} failed: {type(e).__name__}: {e}")
        return fail("internal", f"{type(e).__name__}: {e}")
    return {"id": rid, "ok": True, "result": result}


def run(session: Session, stdin=None, stdout=None) -> int:
    """The NDJSON loop. Returns when stdin closes or after `shutdown`."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def write(obj: dict) -> None:
        stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        stdout.write("\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as e:
            write({"id": None, "ok": False, "error": {"code": "bad-json", "message": str(e)}})
            continue
        response = dispatch(session, request, emit=write)
        write(response)
        if isinstance(request, dict) and request.get("op") == "shutdown":
            return 0
    return 0
