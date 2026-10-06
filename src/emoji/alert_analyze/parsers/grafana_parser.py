"""Parse Grafana alert Slack messages into structured AlertInstance objects."""
import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class AlertInstance:
    alertname: str = ""
    cluster: str = ""
    namespace: str = ""
    pod: str = ""
    description: str = ""
    summary: str = ""
    source_url: str = ""
    dashboard_url: str = ""
    value: str = ""
    raw_labels: dict = field(default_factory=dict)
    raw_annotations: dict = field(default_factory=dict)

    def is_valid(self) -> bool:
        return bool(self.cluster or self.namespace or self.alertname)

    def short_label(self) -> str:
        parts = [p for p in [self.cluster, self.namespace, self.pod] if p]
        return " / ".join(parts) or self.alertname


def _parse_kv_lines(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        m = re.match(r"\s*-\s*(\w+)\s*=\s*(.+)", line)
        if m:
            result[m.group(1).strip()] = m.group(2).strip()
    return result


def parse_grafana_alert(text: str) -> List[AlertInstance]:
    """Parse a Grafana alert Slack message into a list of AlertInstance objects.

    A single Slack message can contain multiple firing alert instances,
    each starting with a 'Value:' line.
    """
    if not text:
        return []

    # Split on 'Value:' boundaries — each block is one alert instance
    raw_blocks = re.split(r"(?:^|\n)Value:", text)

    instances = []
    for block in raw_blocks[1:]:  # first element is the header before any alert
        inst = AlertInstance()

        # Value line (first line of the block)
        first_line = block.split("\n", 1)[0].strip()
        inst.value = first_line

        # Labels section
        labels_m = re.search(r"Labels:(.*?)(?=Annotations:|Source:|$)", block, re.DOTALL)
        if labels_m:
            kv = _parse_kv_lines(labels_m.group(1))
            inst.raw_labels = kv
            inst.alertname = kv.get("alertname", "")
            inst.cluster = kv.get("cluster", "")
            inst.namespace = kv.get("namespace", "")
            inst.pod = kv.get("pod", "")

        # Annotations section
        ann_m = re.search(r"Annotations:(.*?)(?=Source:|Silence:|Dashboard:|$)", block, re.DOTALL)
        if ann_m:
            kv = _parse_kv_lines(ann_m.group(1))
            inst.raw_annotations = kv
            inst.description = kv.get("description", "")
            inst.summary = kv.get("summary", "")

        # URLs
        src_m = re.search(r"Source:\s*(https?://\S+)", block)
        if src_m:
            inst.source_url = src_m.group(1).rstrip(")")

        dash_m = re.search(r"Dashboard:\s*(https?://\S+)", block)
        if dash_m:
            inst.dashboard_url = dash_m.group(1).rstrip(")")

        if inst.is_valid():
            instances.append(inst)

    return instances
