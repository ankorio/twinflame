from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_pools(
    fn: Callable[[T], R],
    items: Iterable[T],
    *,
    jobs: int = 1,
) -> list[R]:
    """Map `fn` across `items` with up to `jobs` parallel workers.

    `jobs <= 1` runs serially in-process (cheaper for small batches and
    keeps stack traces readable for tests). For the diff pipeline `fn` is
    one per pool, so item count is typically small (~one per package).
    """
    item_list = list(items)
    if not item_list:
        return []
    if jobs <= 1 or len(item_list) == 1:
        return [fn(x) for x in item_list]
    with ProcessPoolExecutor(max_workers=jobs) as executor:
        return list(executor.map(fn, item_list))
