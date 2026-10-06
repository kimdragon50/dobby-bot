"""Collect pod logs from Loki HTTP API (via Vector pipeline)."""
import time
from typing import Optional

import requests

from src._shared.config import Config
from src._shared.logger import logger

_BASE_URL = Config.LOKI_URL.rstrip("/")
_LOOKBACK_SECONDS = 1800  # 30 minutes
_LIMIT = 100
_TIMEOUT = (5, 20)


def collect(cluster: str, namespace: str, pod: str) -> Optional[str]:
    """Return recent log lines for the given pod from Loki.

    Returns None if no logs found or on error.
    """
    if not _BASE_URL or not namespace or not pod:
        return None

    end_ns = int(time.time() * 1e9)
    start_ns = end_ns - int(_LOOKBACK_SECONDS * 1e9)

    # Try cluster-scoped selector first, then namespace+pod only
    selectors = [f'{{namespace="{namespace}", pod="{pod}"}}']
    if cluster:
        selectors.insert(0, f'{{cluster="{cluster}", namespace="{namespace}", pod="{pod}"}}')

    for selector in selectors:
        try:
            resp = requests.get(
                f"{_BASE_URL}/loki/api/v1/query_range",
                params={
                    "query": selector,
                    "start": start_ns,
                    "end": end_ns,
                    "limit": _LIMIT,
                    "direction": "backward",
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                result = resp.json().get("data", {}).get("result", [])
                if result:
                    lines = []
                    for stream in result:
                        for _ts, line in stream.get("values", []):
                            lines.append(line)
                    if lines:
                        lines.reverse()  # backward → chronological
                        text = "\n".join(lines[-_LIMIT:])
                        logger.info(f"[Loki] {namespace}/{pod}: {len(lines)} lines")
                        return text
            elif resp.status_code not in (400, 404):
                logger.warning(f"[Loki] {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"[Loki] request error: {e}")

    logger.info(f"[Loki] no logs found for {namespace}/{pod}")
    return None
