from __future__ import annotations

from apkdiff.opcodes import CATEGORY, Category, categorize


def test_table_is_exactly_256_bytes():
    assert len(CATEGORY) == 256


def test_every_opcode_maps_to_a_known_category():
    valid = {c.value for c in Category}
    for op in range(256):
        assert CATEGORY[op] in valid, f"opcode 0x{op:02x} maps to unknown category"


def test_all_thirteen_categories_are_represented():
    seen = set(CATEGORY)
    assert len(Category) == 13
    # Every category we declared should be reachable for at least one opcode,
    # otherwise the table is degenerate.
    for cat in Category:
        assert cat.value in seen, f"category {cat.name} unused"


def test_specific_known_opcodes():
    # Spot-check the spec's example opcodes from the build prompt.
    assert CATEGORY[0x00] == Category.NONE       # nop
    assert CATEGORY[0x11] == Category.END_BB     # return-object
    assert CATEGORY[0x27] == Category.END_BB     # throw
    assert CATEGORY[0x31] == Category.COMPARISON # cmp-long
    assert CATEGORY[0x2D] == Category.COMPARISON # cmpl-float
    assert CATEGORY[0x32] == Category.TEST       # if-eq
    assert CATEGORY[0x33] == Category.TEST       # if-ne
    assert CATEGORY[0x6E] == Category.CALL       # invoke-virtual
    assert CATEGORY[0x71] == Category.CALL       # invoke-static
    assert CATEGORY[0x7B] == Category.ARITH      # neg-int
    assert CATEGORY[0x7C] == Category.ARITH      # not-int
    assert CATEGORY[0x82] == Category.CAST       # int-to-float
    assert CATEGORY[0x83] == Category.CAST       # int-to-double
    assert CATEGORY[0x62] == Category.STATIC_FIELD    # sget-object
    assert CATEGORY[0x63] == Category.STATIC_FIELD    # sget-boolean
    assert CATEGORY[0x5A] == Category.INSTANCE_FIELD  # iput-wide
    assert CATEGORY[0x5D] == Category.INSTANCE_FIELD  # iput-byte
    assert CATEGORY[0x23] == Category.ARRAY      # new-array
    assert CATEGORY[0x24] == Category.ARRAY      # filled-new-array
    assert CATEGORY[0x1A] == Category.STRING     # const-string
    assert CATEGORY[0x1B] == Category.STRING     # const-string/jumbo
    assert CATEGORY[0x06] == Category.MOVE       # move-wide/16
    assert CATEGORY[0x05] == Category.MOVE       # move-wide/from16
    assert CATEGORY[0x13] == Category.INTEGER    # const/16
    assert CATEGORY[0x12] == Category.INTEGER    # const/4


def test_categorize_maps_a_byte_stream():
    raw = bytes([0x00, 0x32, 0x6E, 0x11, 0x12])
    out = categorize(raw)
    assert out == bytes([
        Category.NONE,
        Category.TEST,
        Category.CALL,
        Category.END_BB,
        Category.INTEGER,
    ])
    assert len(out) == len(raw)


def test_categorize_empty_input():
    assert categorize(b"") == b""


def test_unused_opcodes_default_to_none():
    # The Dalvik spec leaves 0x3e–0x43, 0x73, 0x79–0x7a, 0xe3–0xf9 unused;
    # we encode them as NONE rather than skipping them.
    for op in (0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x73, 0x79, 0x7A, 0xE3, 0xF9):
        assert CATEGORY[op] == Category.NONE
