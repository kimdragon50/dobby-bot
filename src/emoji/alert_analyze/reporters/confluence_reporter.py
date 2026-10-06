"""Create Confluence pages for Grafana alert diagnostic reports."""
import json
from datetime import datetime
from typing import Optional

import requests

from src._shared.config import Config
from src._shared.logger import logger


class ConfluenceReporter:
    def __init__(self):
        base = Config.JIRA_SERVER_URL.rstrip("/")
        # Confluence is at {jira_base}/wiki for both Cloud and Server
        self._base = base + "/wiki"
        self._auth = (Config.JIRA_USERNAME, Config.JIRA_API_TOKEN)
        self._space_key = Config.CONFLUENCE_SRE_SPACE_KEY
        self._parent_id = Config.CONFLUENCE_ALERT_PARENT_PAGE_ID
        self._timeout = (10, 30)

    def create_report(
        self,
        alertname: str,
        instances_text: str,
        diagnosis: str,
        raw_data: str,
    ) -> Optional[str]:
        """Create a Confluence page and return its URL, or None on failure."""
        if not self._space_key:
            logger.warning("[Confluence] CONFLUENCE_SRE_SPACE_KEY not set, skipping report")
            return None

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        title = f"[Alert] {alertname} — {now}"
        body = self._build_body(instances_text, diagnosis, raw_data)

        payload = {
            "type": "page",
            "title": title,
            "space": {"key": self._space_key},
            "body": {
                "storage": {
                    "value": body,
                    "representation": "storage",
                }
            },
        }
        if self._parent_id:
            payload["ancestors"] = [{"id": self._parent_id}]

        try:
            resp = requests.post(
                f"{self._base}/rest/api/content",
                json=payload,
                auth=self._auth,
                timeout=self._timeout,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                links = data.get("_links", {})
                web_ui = links.get("webui", "")
                base = links.get("base", self._base)
                url = f"{base}{web_ui}" if web_ui else f"{self._base}/pages/{data.get('id')}"
                logger.info(f"[Confluence] Page created: {url}")
                return url
            else:
                logger.warning(f"[Confluence] Page creation failed: {resp.status_code} {resp.text[:300]}")
                return None
        except Exception as e:
            logger.warning(f"[Confluence] Request error: {e}")
            return None

    def _build_body(self, instances_text: str, diagnosis: str, raw_data: str) -> str:
        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        return f"""
<h2>📋 알림 인스턴스</h2>
<ac:structured-macro ac:name="code">
  <ac:parameter ac:name="language">text</ac:parameter>
  <ac:plain-text-body><![CDATA[{instances_text}]]></ac:plain-text-body>
</ac:structured-macro>

<h2>🔍 LLM 진단 결과</h2>
<p>{esc(diagnosis).replace(chr(10), "<br/>")}</p>

<h2>📊 수집 원시 데이터</h2>
<ac:structured-macro ac:name="code">
  <ac:parameter ac:name="language">text</ac:parameter>
  <ac:plain-text-body><![CDATA[{raw_data}]]></ac:plain-text-body>
</ac:structured-macro>
""".strip()
