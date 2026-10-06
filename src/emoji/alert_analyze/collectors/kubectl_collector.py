"""Collect pod diagnostic data via the Kubernetes Python client."""
from typing import Optional, Tuple

from src._shared.config import Config
from src._shared.logger import logger


def _load_k8s_client(cluster_name: str):
    """Load kubernetes client, selecting context that matches cluster_name."""
    try:
        from kubernetes import client as k8s_client, config as k8s_config

        kubeconfig = Config.KUBECONFIG_PATH or None

        if kubeconfig:
            # Load all contexts from file and pick the best match
            contexts, _ = k8s_config.list_kube_config_contexts(config_file=kubeconfig)
            matched = None
            for ctx in contexts:
                ctx_name = ctx.get("name", "")
                if cluster_name and cluster_name in ctx_name:
                    matched = ctx_name
                    break

            k8s_config.load_kube_config(config_file=kubeconfig, context=matched)
        else:
            # Fallback: in-cluster config
            k8s_config.load_incluster_config()

        return k8s_client.CoreV1Api()

    except Exception as e:
        logger.warning(f"[KubectlCollector] Failed to load k8s client: {e}")
        return None


def collect(cluster: str, namespace: str, pod: str) -> Tuple[str, str]:
    """Return (describe_text, logs_text) for the given pod.

    Returns empty strings on failure — callers should handle gracefully.
    """
    describe_text = ""
    logs_text = ""

    if not namespace or not pod:
        return describe_text, logs_text

    v1 = _load_k8s_client(cluster)
    if v1 is None:
        return describe_text, logs_text

    # kubectl describe pod
    try:
        pod_obj = v1.read_namespaced_pod(name=pod, namespace=namespace)
        describe_text = _pod_to_describe_text(pod_obj)
        logger.info(f"[KubectlCollector] describe pod: {namespace}/{pod} ({len(describe_text)} chars)")
    except Exception as e:
        logger.warning(f"[KubectlCollector] describe pod failed: {e}")

    # kubectl logs: try previous container first, then current
    for previous in (True, False):
        try:
            logs_text = v1.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                tail_lines=100,
                previous=previous,
            )
            logger.info(f"[KubectlCollector] logs ({'previous' if previous else 'current'}): {len(logs_text)} chars")
            break
        except Exception as e:
            if not previous:
                logger.warning(f"[KubectlCollector] logs failed: {e}")

    # kubectl get events (pod-scoped, last 15)
    try:
        events = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod}",
        )
        if events.items:
            event_lines = []
            for e in sorted(events.items, key=lambda x: x.last_timestamp or "", reverse=False)[-15:]:
                event_lines.append(f"  [{e.type}] {e.reason}: {e.message}")
            events_section = "Events (pod):\n" + "\n".join(event_lines)
            describe_text = (describe_text + "\n\n" + events_section).strip() if describe_text else events_section
            logger.info(f"[KubectlCollector] {len(events.items)} events for {namespace}/{pod}")
    except Exception as e:
        logger.warning(f"[KubectlCollector] events failed: {e}")

    return describe_text, logs_text


def _pod_to_describe_text(pod) -> str:
    """Convert a kubernetes V1Pod object to a human-readable describe-like string."""
    lines = []
    meta = pod.metadata
    spec = pod.spec
    status = pod.status

    lines.append(f"Name:      {meta.name}")
    lines.append(f"Namespace: {meta.namespace}")
    lines.append(f"Node:      {spec.node_name or 'unknown'}")
    lines.append(f"Phase:     {status.phase or 'unknown'}")

    if status.conditions:
        lines.append("Conditions:")
        for c in status.conditions:
            lines.append(f"  {c.type}: {c.status}")

    if status.container_statuses:
        lines.append("Containers:")
        for cs in status.container_statuses:
            lines.append(f"  {cs.name}:")
            lines.append(f"    Ready:         {cs.ready}")
            lines.append(f"    Restart Count: {cs.restart_count}")
            if cs.state:
                if cs.state.waiting:
                    lines.append(f"    State:         Waiting — {cs.state.waiting.reason}: {cs.state.waiting.message or ''}")
                elif cs.state.running:
                    lines.append(f"    State:         Running since {cs.state.running.started_at}")
                elif cs.state.terminated:
                    t = cs.state.terminated
                    lines.append(f"    State:         Terminated — {t.reason} (exit {t.exit_code})")
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                lines.append(f"    Last State:    Terminated — {t.reason} (exit {t.exit_code}) at {t.finished_at}")

    if status.events:
        lines.append("Events:")
        for e in (status.events or [])[-10:]:
            lines.append(f"  {e.type}  {e.reason}  {e.message}")

    return "\n".join(lines)
