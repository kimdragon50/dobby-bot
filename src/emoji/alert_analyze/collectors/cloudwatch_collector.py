"""Collect CloudWatch Container Insights metrics for a pod."""
from datetime import datetime, timedelta, timezone
from typing import Dict

import boto3

from src._shared.config import Config
from src._shared.logger import logger

# EKS Container Insights metrics namespace
_CW_NAMESPACE = "ContainerInsights"
_PERIOD = 300  # 5-minute data points
_LOOKBACK_MINUTES = 30


def collect(cluster: str, namespace: str, pod: str) -> Dict[str, str]:
    """Return a dict of metric_name → readable summary string.

    Returns empty dict on failure.
    """
    if not cluster or not namespace or not pod:
        return {}

    try:
        session = boto3.Session(
            aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
            region_name=Config.AWS_REGION,
        )
        cw = session.client("cloudwatch")

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=_LOOKBACK_MINUTES)

        dimensions = [
            {"Name": "ClusterName", "Value": cluster},
            {"Name": "Namespace", "Value": namespace},
            {"Name": "PodName", "Value": pod},
        ]

        metrics = {}
        for metric_name, unit_label in [
            ("pod_cpu_utilization", "CPU %"),
            ("pod_memory_utilization", "Memory %"),
        ]:
            try:
                resp = cw.get_metric_statistics(
                    Namespace=_CW_NAMESPACE,
                    MetricName=metric_name,
                    Dimensions=dimensions,
                    StartTime=start,
                    EndTime=end,
                    Period=_PERIOD,
                    Statistics=["Average", "Maximum"],
                )
                datapoints = sorted(resp.get("Datapoints", []), key=lambda x: x["Timestamp"])
                if datapoints:
                    avg = datapoints[-1]["Average"]
                    mx = max(d["Maximum"] for d in datapoints)
                    metrics[unit_label] = f"avg={avg:.1f}%, max={mx:.1f}% (last {_LOOKBACK_MINUTES}m)"
                else:
                    metrics[unit_label] = "no data"
            except Exception as e:
                logger.warning(f"[CWCollector] {metric_name} failed: {e}")
                metrics[unit_label] = "unavailable"

        logger.info(f"[CWCollector] metrics for {cluster}/{namespace}/{pod}: {metrics}")
        return metrics

    except Exception as e:
        logger.warning(f"[CWCollector] Failed to initialize CloudWatch client: {e}")
        return {}
