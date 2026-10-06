"""Alert Analyze Handler — :op_analyze: reaction on Grafana alert messages.

Flow:
  1. :op_analyze: emoji on Grafana alert Slack message
  2. Parse alert instances from message text
  3. Collect: kubectl describe + logs, CloudWatch metrics (per instance, max 3)
  4. LLM diagnosis
  5. Write Confluence report
  6. Post Slack summary with [Confluence] + [Jira 티켓 생성] buttons
"""
import asyncio
import json
import threading
from typing import List, Optional

from slack_sdk.errors import SlackApiError

from src._shared.config import Config
from src._shared.jira import JiraService
from src._shared.llm import LLMService
from src._shared.logger import logger

from .parsers.grafana_parser import parse_grafana_alert, AlertInstance
from .collectors import kubectl_collector, cloudwatch_collector, prometheus_collector, loki_collector
from .reporters.confluence_reporter import ConfluenceReporter


# ── LLM Prompts ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are an expert SRE (Site Reliability Engineer) analyzing a Kubernetes alert.

You have access to:
- Grafana alert metadata (cluster, namespace, pod, alertname)
- kubectl describe pod output
- Recent pod logs (tail 100 lines)
- CloudWatch Container Insights metrics (CPU/Memory)

Your job:
1. Identify the most likely root cause
2. Assess severity (Critical / High / Medium / Low)
3. Recommend the most appropriate remediation action

Output EXACTLY in this format (Korean, plain text only — no markdown, no ##, no **):

SEVERITY: <Critical|High|Medium|Low>

CAUSE:
<2-3 sentences explaining what happened and why>

ACTIONS:
1. <most important action>
2. <next action>
3. <additional action if needed>
""".strip()

_HUMAN_PROMPT = """
## 알림 정보
{alert_summary}

## kubectl describe pod (이벤트 포함)
{describe}

## Pod Logs (kubectl 또는 Loki, 최근 100줄)
{logs}

## Prometheus Metrics (최근 30분)
{metrics}

위 정보를 바탕으로 SRE 진단 리포트를 작성해주세요.
""".strip()


_SEVERITY_EMOJI = {
    "Critical": "🔴", "High": "🔴",
    "Medium": "🟡",
    "Low": "🟢",
    "심각": "🔴", "높음": "🔴",
    "보통": "🟡", "중간": "🟡",
    "낮음": "🟢",
}

_SEVERITY_KO = {
    "Critical": "심각", "High": "높음",
    "Medium": "보통",
    "Low": "낮음",
}


class AlertAnalyzeHandler:
    """Handler for :op_analyze: — analyzes Grafana alerts posted in Slack."""

    REACTION_NAME = "op_analyze"
    ACTION_CREATE_JIRA = "alert_analyze_create_jira"
    JIRA_PROJECT = Config.OPS_JIRA_PROJECT_KEY
    MAX_INSTANCES = 3  # analyze at most this many alert instances per message

    def __init__(self, jira_service: JiraService, llm_service: LLMService):
        self.jira = jira_service
        self.llm = llm_service
        self._confluence = ConfluenceReporter()

    def register_handlers(self, app) -> None:
        """Register block_actions handler for the Jira ticket button."""
        app.action(self.ACTION_CREATE_JIRA)(self._handle_create_jira_action)

    # ── Reaction entry point ────────────────────────────────────────────────

    def handle_reaction_added(self, event, client, say) -> None:
        try:
            user_id = event.get("user")
            item = event.get("item", {})
            channel_id = item.get("channel")
            ts = item.get("ts")

            if not channel_id or not ts or not user_id:
                return

            logger.info(f"[AlertAnalyze] Triggered by user={user_id}, channel={channel_id}, ts={ts}")
            threading.Thread(
                target=self._pipeline,
                args=(client, channel_id, ts, user_id),
                name=f"alert_analyze_{channel_id}_{ts}",
                daemon=True,
            ).start()
        except Exception as e:
            logger.exception(f"[AlertAnalyze] handle_reaction_added error: {e}")

    # ── Background pipeline ─────────────────────────────────────────────────

    def _pipeline(self, client, channel_id: str, ts: str, user_id: str) -> None:
        root_ts = ts
        root_msg = None
        try:
            # 1. Resolve root message ts (reuse the fetched message to avoid a second API call)
            try:
                resp = client.conversations_replies(channel=channel_id, ts=ts, limit=1, inclusive=True)
                msgs = resp.get("messages", [])
                if msgs:
                    root_ts = msgs[0].get("ts", ts)
                    root_msg = msgs[0]
            except SlackApiError as e:
                if "not_in_channel" in str(e):
                    self._try_join_channel(client, channel_id)
                    try:
                        resp = client.conversations_replies(channel=channel_id, ts=ts, limit=1, inclusive=True)
                        msgs = resp.get("messages", [])
                        if msgs:
                            root_ts = msgs[0].get("ts", ts)
                            root_msg = msgs[0]
                    except SlackApiError as e2:
                        logger.warning(f"[AlertAnalyze] Failed to resolve root_ts after join: {e2}")
                else:
                    logger.warning(f"[AlertAnalyze] Failed to resolve root_ts: {e}")

            # 2. Extract message text (Grafana uses attachments/blocks, not plain text)
            # conversations_replies may omit attachments for bot messages — fall back to
            # conversations_history which reliably returns the full message payload.
            message_text = self._extract_message_text(root_msg) if root_msg else ""
            if not message_text:
                message_text = self._get_message_text(client, channel_id, root_ts)
            if not message_text:
                client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="⚠️ 메시지 내용을 읽을 수 없습니다."
                )
                return

            # 3. Parse Grafana alert instances
            instances = parse_grafana_alert(message_text)
            if not instances:
                client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="⚠️ Grafana 알림 형식을 인식할 수 없습니다. Grafana 알림 메시지에만 :op_analyze:를 달아주세요."
                )
                return

            instances = instances[: self.MAX_INSTANCES]
            alertname = instances[0].alertname or "Unknown Alert"

            # 4. Acknowledge with "analyzing..." message
            loading_resp = client.chat_postMessage(
                channel=channel_id,
                thread_ts=root_ts,
                text=f"🔍 *{alertname}* — {len(instances)}개 인스턴스 분석 중...",
            )
            loading_ts = loading_resp.get("ts")

            # 5. Collect diagnostic data
            all_describe, all_logs, all_metrics = [], [], []
            for inst in instances:
                label = inst.short_label()

                # kubectl describe + events + previous logs
                describe, logs = kubectl_collector.collect(inst.cluster, inst.namespace, inst.pod)

                # Loki: use when kubectl logs empty (pod already gone)
                if not logs:
                    loki_logs = loki_collector.collect(inst.cluster, inst.namespace, inst.pod)
                    if loki_logs:
                        logs = f"[Loki]\n{loki_logs}"

                # Prometheus: primary metrics source
                metrics = prometheus_collector.collect(inst.cluster, inst.namespace, inst.pod)
                # CloudWatch fallback if Prometheus has no data
                if not any(v != "(데이터 없음)" for v in metrics.values()):
                    cw = cloudwatch_collector.collect(inst.cluster, inst.namespace, inst.pod)
                    if cw:
                        metrics = {f"[CW] {k}": v for k, v in cw.items()}

                all_describe.append(f"=== {label} ===\n{describe}" if describe else f"=== {label} ===\n(데이터 없음)")
                all_logs.append(f"=== {label} ===\n{logs}" if logs else f"=== {label} ===\n(로그 없음)")
                metrics_str = ", ".join(f"{k}: {v}" for k, v in metrics.items()) if metrics else "(데이터 없음)"
                all_metrics.append(f"{label}: {metrics_str}")

            alert_summary = self._format_alert_summary(instances)
            describe_combined = "\n\n".join(all_describe)[:8000]
            logs_combined = "\n\n".join(all_logs)[:5000]
            metrics_combined = "\n".join(all_metrics)

            # 6. LLM diagnosis
            human = _HUMAN_PROMPT.format(
                alert_summary=alert_summary,
                describe=describe_combined or "(수집 실패)",
                logs=logs_combined or "(수집 실패)",
                metrics=metrics_combined or "(수집 실패)",
            )
            try:
                diagnosis = asyncio.run(self.llm.invoke(_SYSTEM_PROMPT, human, {}))
            except Exception as e:
                logger.error(f"[AlertAnalyze] LLM failed: {e}")
                diagnosis = f"LLM 분석 실패: {e}"

            # 7. Create Confluence report
            raw_data = f"=== kubectl describe ===\n{describe_combined}\n\n=== Logs ===\n{logs_combined}\n\n=== Metrics ===\n{metrics_combined}"
            confluence_url = self._confluence.create_report(
                alertname=alertname,
                instances_text=alert_summary,
                diagnosis=diagnosis,
                raw_data=raw_data,
            )

            # 8. Build button value payload (max ~1800 chars for Slack safety)
            parsed_diag = self._parse_diagnosis(diagnosis)
            button_payload = json.dumps({
                "alertname": alertname[:100],
                "cluster": instances[0].cluster[:60],
                "namespace": instances[0].namespace[:60],
                "pod": instances[0].pod[:100],
                "summary": (instances[0].summary or instances[0].description)[:200],
                "channel_id": channel_id,
                "thread_ts": root_ts,
                "confluence_url": confluence_url or "",
                "severity": parsed_diag["severity"][:20],
                "diagnosis": parsed_diag["cause"][:400],
            })

            # 9. Post final result
            blocks = self._build_result_blocks(
                alertname=alertname,
                instances=instances,
                diagnosis=diagnosis,
                confluence_url=confluence_url,
                button_payload=button_payload,
            )

            # Replace the loading message with the result
            try:
                if loading_ts:
                    client.chat_update(
                        channel=channel_id,
                        ts=loading_ts,
                        text=f"🔍 {alertname} 분석 완료",
                        blocks=blocks,
                    )
                else:
                    client.chat_postMessage(
                        channel=channel_id, thread_ts=root_ts, blocks=blocks,
                        text=f"🔍 {alertname} 분석 완료",
                    )
            except SlackApiError:
                client.chat_postMessage(
                    channel=channel_id, thread_ts=root_ts, blocks=blocks,
                    text=f"🔍 {alertname} 분석 완료",
                )

        except Exception as e:
            logger.exception(f"[AlertAnalyze] Pipeline error: {e}")
            try:
                client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text=f"⚠️ 분석 중 오류 발생: {str(e)[:200]}"
                )
            except Exception:
                pass

    # ── Jira button action ──────────────────────────────────────────────────

    def _handle_create_jira_action(self, ack, body, client) -> None:
        ack()
        try:
            action = body["actions"][0]
            payload = json.loads(action.get("value", "{}"))
            channel_id = payload.get("channel_id", body["channel"]["id"])
            thread_ts = payload.get("thread_ts")
            alertname = payload.get("alertname", "Alert")
            confluence_url = payload.get("confluence_url", "")
            diagnosis = payload.get("diagnosis", "")
            namespace = payload.get("namespace", "")
            pod = payload.get("pod", "")

            summary = f"[Alert] {alertname} — {namespace}/{pod}".strip(" —/")
            description = (
                f"h2. 알림 정보\n"
                f"*Alert:* {alertname}\n"
                f"*Namespace:* {namespace}\n"
                f"*Pod:* {pod}\n\n"
                f"h2. 진단 요약\n{diagnosis}\n\n"
            )
            if confluence_url:
                description += f"h2. 상세 리포트\n{confluence_url}\n"

            issue = self.jira.client.create_issue(fields={
                "project": {"key": self.JIRA_PROJECT},
                "summary": summary[:255],
                "description": description,
                "issuetype": {"name": "Task"},
            })
            ticket_key = issue.key
            ticket_url = f"{Config.JIRA_SERVER_URL}/browse/{ticket_key}"
            logger.info(f"[AlertAnalyze] Jira ticket created: {ticket_key}")

            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"🎫 Jira 티켓 생성 완료: *<{ticket_url}|{ticket_key}>* {summary}",
            )
        except Exception as e:
            logger.exception(f"[AlertAnalyze] Jira action failed: {e}")
            try:
                client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=body["user"]["id"],
                    text=f"⚠️ Jira 티켓 생성 실패: {str(e)[:200]}",
                )
            except Exception:
                pass

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _parse_diagnosis(self, text: str) -> dict:
        """Parse structured LLM output (SEVERITY/CAUSE/ACTIONS) into a dict."""
        import re
        result = {"severity": "", "cause": text.strip(), "actions": []}

        m = re.search(r"SEVERITY:\s*(.+)", text)
        if m:
            result["severity"] = m.group(1).strip()

        m = re.search(r"CAUSE:\s*\n(.*?)(?=\nACTIONS:|$)", text, re.DOTALL)
        if m:
            result["cause"] = m.group(1).strip()

        m = re.search(r"ACTIONS:\s*\n(.*?)$", text, re.DOTALL)
        if m:
            result["actions"] = [
                re.sub(r"^\d+\.\s*", "", line).strip()
                for line in m.group(1).splitlines()
                if line.strip()
            ]

        return result

    def _try_join_channel(self, client, channel_id: str) -> bool:
        try:
            client.conversations_join(channel=channel_id)
            logger.info(f"[AlertAnalyze] Joined channel {channel_id}")
            return True
        except SlackApiError as e:
            logger.warning(f"[AlertAnalyze] Cannot join channel {channel_id}: {e}")
            return False

    def _extract_message_text(self, msg: dict) -> str:
        """Extract text from a Slack message, including attachments and blocks.

        Grafana alerts often send content in legacy attachments (text/fallback)
        rather than the top-level text field.
        """
        parts = []
        if msg.get("text"):
            parts.append(msg["text"])
        for att in msg.get("attachments", []):
            for key in ("pretext", "title", "text", "fallback"):
                val = att.get(key, "")
                if val:
                    parts.append(val)
        for block in msg.get("blocks", []):
            btype = block.get("type", "")
            if btype in ("section", "header"):
                text_obj = block.get("text") or {}
                if text_obj.get("text"):
                    parts.append(text_obj["text"])
        return "\n".join(filter(None, parts))

    def _get_message_text(self, client, channel_id: str, ts: str) -> str:
        for attempt in range(2):
            try:
                resp = client.conversations_history(channel=channel_id, latest=ts, limit=1, inclusive=True)
                msgs = resp.get("messages", [])
                if msgs:
                    return self._extract_message_text(msgs[0])
                return ""
            except SlackApiError as e:
                if "not_in_channel" in str(e) and attempt == 0:
                    self._try_join_channel(client, channel_id)
                    continue
                logger.warning(f"[AlertAnalyze] conversations_history failed: {e}")
                return ""
        return ""

    def _format_alert_summary(self, instances: List[AlertInstance]) -> str:
        lines = []
        for i, inst in enumerate(instances, 1):
            lines.append(f"[{i}] alertname={inst.alertname}")
            lines.append(f"    cluster={inst.cluster}, namespace={inst.namespace}, pod={inst.pod}")
            if inst.description:
                lines.append(f"    description={inst.description}")
            if inst.source_url:
                lines.append(f"    source={inst.source_url}")
        return "\n".join(lines)

    def _build_result_blocks(
        self,
        alertname: str,
        instances: List[AlertInstance],
        diagnosis: str,
        confluence_url: Optional[str],
        button_payload: str,
    ) -> list:
        parsed = self._parse_diagnosis(diagnosis)
        severity = parsed["severity"]
        cause = parsed["cause"] or diagnosis[:500]
        actions = parsed["actions"]

        severity_emoji = _SEVERITY_EMOJI.get(severity, "⚪")
        severity_ko = _SEVERITY_KO.get(severity, severity)
        severity_line = f"{severity_emoji} 심각도: *{severity_ko}*" if severity else "⚪ 심각도: 알 수 없음"

        instance_lines = "\n".join(f"• {inst.short_label()}" for inst in instances)

        if actions:
            circled = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
            action_lines = "\n".join(
                f"{circled[i] if i < len(circled) else f'{i+1}.'} {a}"
                for i, a in enumerate(actions)
            )
        else:
            action_lines = "(없음)"

        actions_text = f"*🛠 권장 조치*\n{action_lines}"
        if confluence_url:
            actions_text += f"\n\n<{confluence_url}|📄 진단 상세 (Confluence)>"

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*🔍 {alertname}* 분석 완료\n{instance_lines}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": severity_line},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📌 원인 분석*\n{cause[:1000]}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": actions_text},
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🎫 Jira 티켓 생성"},
                        "style": "primary",
                        "action_id": self.ACTION_CREATE_JIRA,
                        "value": button_payload[:1990],
                    }
                ],
            },
        ]
        return blocks

