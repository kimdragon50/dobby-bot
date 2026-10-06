"""분산 락 — src/_shared/lock.py 로 이전했다.

기존 import 경로를 쓰는 코드를 위해 재노출만 한다.
신규 코드는 `from src._shared.lock import DistributedLock` 을 쓸 것.
"""
from src._shared.lock import (  # noqa: F401
    DEFAULT_TTL_SECONDS,
    TABLE_NAME,
    DistributedLock,
)

__all__ = ["DistributedLock", "TABLE_NAME", "DEFAULT_TTL_SECONDS"]
