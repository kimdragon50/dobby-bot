"""Collect pod metrics from Prometheus HTTP API."""
import time
from typing import Dict

import requests

from src._shared.config import Config
from src._shared.logger import logger

_BASE_URL = Config.PROMETHEUS_URL.rstrip("/")
_LOOKBACK_SECONDS = 1800  # 30 minutes
_STEP = "60s"
_TIMEOUT = (5, 15)


def _query_range(query: str, start: float, end: float) -> list:
    try:
        resp = requests.get(
            f"{_BASE_URL}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": _STEP},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("result", [])
        logger.warning(f"[Prometheus] query_range {resp.status_code}: {resp.text[:200]}")
        return []
    except Exception as e:
        logger.warning(f"[Prometheus] query_range error: {e}")
        return []


def _query_instant(query: str) -> list:
    try:
        resp = requests.get(
            f"{_BASE_URL}/api/v1/query",
            params={"query": query},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("result", [])
        return []
    except Exception as e:
        logger.warning(f"[Prometheus] query_instant error: {e}")
        return []


def _with_cluster_fallback(query_fn, query_with_cluster: str, query_without: str, *args):
    """Try query with cluster label first, fall back to without."""
    result = query_fn(query_with_cluster, *args)
    if not result:
        result = query_fn(query_without, *args)
    return result


def _series_summary(series: list) -> dict:
    values = [float(v) for r in series for _, v in r.get("values", []) if v not in ("NaN", "+Inf")]
    if not values:
        return {}
    return {"avg": sum(values) / len(values), "max": max(values), "min": min(values)}


def collect(cluster: str, namespace: str, pod: str) -> Dict[str, str]:
    """Return metrics dict for the given pod from Prometheus."""
    if not _BASE_URL or not namespace or not pod:
        return {}

    end = time.time()
    start = end - _LOOKBACK_SECONDS

    cluster_sel = f', cluster="{cluster}"' if cluster else ""
    pod_sel = f'namespace="{namespace}", pod="{pod}"'
    container_sel = f'{pod_sel}, container!="", container!="POD"'

    metrics: Dict[str, str] = {}

    # CPU usage rate
    cpu_q = f'rate(container_cpu_usage_seconds_total{{{container_sel}{cluster_sel}}}[5m])'
    cpu_q_no_cl = f'rate(container_cpu_usage_seconds_total{{{container_sel}}}[5m])'
    cpu_r = _with_cluster_fallback(_query_range, cpu_q, cpu_q_no_cl, start, end)
    s = _series_summary(cpu_r)
    metrics["CPU 사용률"] = f"avg={s['avg']*100:.1f}%, max={s['max']*100:.1f}%" if s else "(데이터 없음)"

    # Memory working set
    mem_q = f'container_memory_working_set_bytes{{{container_sel}{cluster_sel}}}'
    mem_q_no_cl = f'container_memory_working_set_bytes{{{container_sel}}}'
    mem_r = _with_cluster_fallback(_query_range, mem_q, mem_q_no_cl, start, end)
    if mem_r:
        mb_vals = [float(v) / 1024 / 1024 for r in mem_r for _, v in r.get("values", []) if v not in ("NaN", "+Inf")]
        if mb_vals:
            metrics["메모리 사용량"] = f"avg={sum(mb_vals)/len(mb_vals):.0f}MB, max={max(mb_vals):.0f}MB"
        else:
            metrics["메모리 사용량"] = "(데이터 없음)"
    else:
        metrics["메모리 사용량"] = "(데이터 없음)"

    # Memory limit (for OOM risk assessment)
    lim_q = f'container_spec_memory_limit_bytes{{{container_sel}{cluster_sel}, container!=""}}'
    lim_q_no_cl = f'container_spec_memory_limit_bytes{{{container_sel}}}'
    lim_r = _with_cluster_fallback(_query_instant, lim_q, lim_q_no_cl)
    if lim_r:
        limits = [float(r["value"][1]) / 1024 / 1024 for r in lim_r if r.get("value") and float(r["value"][1]) > 0]
        if limits:
            metrics["메모리 제한"] = f"{max(limits):.0f}MB"

    # Restart count
    rst_q = f'kube_pod_container_status_restarts_total{{{pod_sel}{cluster_sel}}}'
    rst_q_no_cl = f'kube_pod_container_status_restarts_total{{{pod_sel}}}'
    rst_r = _with_cluster_fallback(_query_instant, rst_q, rst_q_no_cl)
    if rst_r:
        total = sum(float(r["value"][1]) for r in rst_r if r.get("value"))
        metrics["재시작 횟수"] = f"{int(total)}회"

    # OOMKilled detection
    oom_q = f'kube_pod_container_status_last_terminated_reason{{{pod_sel}{cluster_sel}, reason="OOMKilled"}}'
    oom_q_no_cl = f'kube_pod_container_status_last_terminated_reason{{{pod_sel}, reason="OOMKilled"}}'
    oom_r = _with_cluster_fallback(_query_instant, oom_q, oom_q_no_cl)
    if oom_r and any(float(r["value"][1]) > 0 for r in oom_r if r.get("value")):
        metrics["OOMKilled"] = "⚠️ 감지됨"

    logger.info(f"[Prometheus] {namespace}/{pod}: {metrics}")
    return metrics
