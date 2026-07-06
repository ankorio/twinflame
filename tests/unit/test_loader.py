"""loader.py pure-function helpers (no APK/DEX binary needed).

`loader.py`'s on-disk DEX/APK round-trip is smoke-tested only (see README);
these test the descriptor-parsing helpers directly since they don't need
androguard at all.
"""

from __future__ import annotations

from twinflame.loader import _count_params, _parse_proto


def test_count_params_compact_descriptor_no_spaces():
    # The raw JVM/dex descriptor form has no separators between args.
    assert _count_params("ILjava/lang/String;") == 2
    assert _count_params("") == 0
    assert _count_params("I") == 1


def test_count_params_ignores_androguards_space_separated_pretty_format():
    # androguard's EncodedMethod.get_descriptor() documents its own format
    # as "(A A A ...)R" — space-separated args, not the compact JVM form.
    # A naive char-scan miscounts the space as its own zero-width arg.
    assert _count_params("I Ljava/lang/String;") == 2
    assert _count_params("Ljava/lang/String; I") == 2
    assert _count_params("I Ljava/lang/String; Ljava/lang/Throwable;") == 3


def test_parse_proto_arg_count_matches_real_param_count():
    # Regression: a real 2-arg constructor descriptor from androguard was
    # previously miscounted as arg_count=3 (the space counted as an arg).
    arg_count, ret = _parse_proto("(I Ljava/lang/String;)V")
    assert arg_count == 2
    assert ret == "V"


def test_parse_proto_strips_return_type_whitespace():
    # Defensive: return-type side shouldn't carry stray whitespace either.
    _, ret = _parse_proto("(I)Ljava/lang/String; ")
    assert ret == "Ljava/lang/String;"
