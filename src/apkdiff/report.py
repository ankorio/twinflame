from __future__ import annotations

import json
from typing import Iterable

from .model import Match


def split_added_deleted(matches: Iterable[Match]) -> tuple[list[Match], list[Match], list[Match]]:
    """Partition matches into (paired, deleted, added)."""
    paired: list[Match] = []
    deleted: list[Match] = []
    added: list[Match] = []
    for m in matches:
        if m.is_paired:
            paired.append(m)
        elif m.is_deleted:
            deleted.append(m)
        elif m.is_added:
            added.append(m)
    return paired, deleted, added


def render_human(matches: Iterable[Match], *, only_changed: bool = True) -> str:
    paired, deleted, added = split_added_deleted(matches)
    lines: list[str] = []
    for m in paired:
        if only_changed and m.distance >= 1.0:
            continue
        lines.append(f"[+] {m.lhs.info} | {m.rhs.info} -> {m.distance:1.4f}")
    for m in deleted:
        lines.append(f"[-] {m.lhs.info}")
    for m in added:
        lines.append(f"[*] {m.rhs.info}")
    return "\n".join(lines)


def render_json(matches: Iterable[Match]) -> str:
    rows = []
    for m in matches:
        rows.append({
            "lhs": m.lhs.info if m.lhs else None,
            "rhs": m.rhs.info if m.rhs else None,
            "distance": m.distance,
            "breakdown": m.breakdown,
            "status": "added" if m.is_added else "deleted" if m.is_deleted else "paired",
        })
    return json.dumps(rows, indent=2, sort_keys=True)
