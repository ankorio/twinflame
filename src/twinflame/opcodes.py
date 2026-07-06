from __future__ import annotations

from enum import IntEnum


class Category(IntEnum):
    NONE = 0
    TEST = 1
    END_BB = 2
    COMPARISON = 3
    CALL = 4
    ARITH = 5
    CAST = 6
    STATIC_FIELD = 7
    INSTANCE_FIELD = 8
    ARRAY = 9
    STRING = 10
    MOVE = 11
    INTEGER = 12


_NONE = Category.NONE
_TEST = Category.TEST
_END = Category.END_BB
_CMP = Category.COMPARISON
_CALL = Category.CALL
_ARITH = Category.ARITH
_CAST = Category.CAST
_SFIELD = Category.STATIC_FIELD
_IFIELD = Category.INSTANCE_FIELD
_ARR = Category.ARRAY
_STR = Category.STRING
_MOV = Category.MOVE
_INT = Category.INTEGER


def _build_table() -> bytes:
    t = [_NONE] * 256

    # nop
    t[0x00] = _NONE

    # move family (0x01–0x0d)
    for op in range(0x01, 0x0E):
        t[op] = _MOV

    # return family (0x0e–0x11)
    for op in range(0x0E, 0x12):
        t[op] = _END

    # const family — numeric constants
    for op in range(0x12, 0x1A):
        t[op] = _INT

    # const-string variants
    t[0x1A] = _STR
    t[0x1B] = _STR

    # const-class — no dedicated category; keep neutral
    t[0x1C] = _NONE

    # monitor-enter / exit
    t[0x1D] = _NONE
    t[0x1E] = _NONE

    # check-cast / instance-of
    t[0x1F] = _CAST
    t[0x20] = _CAST

    # array operations and instantiation
    t[0x21] = _ARR  # array-length
    t[0x22] = _NONE  # new-instance — no obvious bucket
    t[0x23] = _ARR  # new-array
    t[0x24] = _ARR  # filled-new-array
    t[0x25] = _ARR  # filled-new-array/range
    t[0x26] = _ARR  # fill-array-data

    # throw and unconditional branches
    t[0x27] = _END
    t[0x28] = _END  # goto
    t[0x29] = _END  # goto/16
    t[0x2A] = _END  # goto/32
    t[0x2B] = _END  # packed-switch
    t[0x2C] = _END  # sparse-switch

    # comparisons
    for op in range(0x2D, 0x32):
        t[op] = _CMP

    # conditional branches
    for op in range(0x32, 0x3E):
        t[op] = _TEST

    # 0x3E–0x43: unused

    # array get/put
    for op in range(0x44, 0x52):
        t[op] = _ARR

    # instance field get/put
    for op in range(0x52, 0x60):
        t[op] = _IFIELD

    # static field get/put
    for op in range(0x60, 0x6E):
        t[op] = _SFIELD

    # invoke family
    for op in range(0x6E, 0x73):
        t[op] = _CALL
    # 0x73 unused
    for op in range(0x74, 0x79):
        t[op] = _CALL
    # 0x79–0x7a unused

    # unary arithmetic (neg/not)
    for op in range(0x7B, 0x81):
        t[op] = _ARITH

    # type conversions
    for op in range(0x81, 0x90):
        t[op] = _CAST

    # binary arithmetic + /2addr + literal variants
    for op in range(0x90, 0xE3):
        t[op] = _ARITH

    # 0xE3–0xF9 unused/reserved

    # invoke-polymorphic / invoke-custom
    t[0xFA] = _CALL
    t[0xFB] = _CALL
    t[0xFC] = _CALL
    t[0xFD] = _CALL

    # const-method-handle / const-method-type — no dedicated bucket
    t[0xFE] = _NONE
    t[0xFF] = _NONE

    return bytes(c.value for c in t)


CATEGORY: bytes = _build_table()
"""256-byte lookup table: CATEGORY[opcode] = Category value (0–12)."""


def categorize(bytecode: bytes) -> bytes:
    """Map a raw opcode stream through CATEGORY in one pass."""
    return bytes(CATEGORY[b] for b in bytecode)


def method_categories(m) -> bytes:
    """Abstract-opcode sequence for a method: the cached `abstract` when the
    method came from a prepared record (raw bytecode dropped), else derived from
    `bytecode` on demand. Both paths yield the identical byte sequence."""
    return m.abstract if m.abstract is not None else categorize(m.bytecode)
