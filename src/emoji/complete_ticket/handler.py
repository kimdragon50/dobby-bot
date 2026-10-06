"""Complete Ticket Handler - Mark SRE Jira tickets as done via Slack emoji reaction

Triggered by :op_sre_complete: reaction.
Searches SRE project for Jira tickets linked to the Slack thread and transitions them to Done.
"""
import threading
from typing import List, Dict

from slack_sdk.errors import SlackApiError

from src._shared.config import Config
from src._shared.logger import logger
from src._shared.jira import JiraService


class CompleteTicketHandler:
    """Handler for :op_sre_complete: - marks linked SRE Jira tickets as done"""

    REACTION_NAME = "op_sre_complete"
    PROJECT_KEY = Config.OPS_JIRA_PROJECT_KEY

    def __init__(self, jira_service: JiraService):
        self.jira = jira_service

    def handle_reaction_added(self, event, client, say):
        """Handle reaction_added event"""
        try:
            user_id = event.get("user")
            item = event.get("item", {})
            channel_id = item.get("channel")
            ts = item.get("ts")

            logger.info(f"[Complete] Triggered by user={user_id}, channel={channel_id}, ts={ts}")

            if not channel_id or not ts or not user_id:
                return

            thread = threading.Thread(
                target=self._pipeline_complete,
                args=(client, channel_id, ts, user_id),
                name=f"complete_{channel_id}_{ts}",
                daemon=True
            )
            thread.start()

        except Exception as e:
            logger.exception(f"[Complete] Error in reaction_added handler: {e}")

    def _pipeline_complete(self, client, channel_id: str, ts: str, user_id: str) -> None:
        """Background: find linked SRE Jira tickets and transition them to done"""
        try:
            # Resolve thread root ts
            root_ts = ts
            try:
                resp = client.conversations_replies(channel=channel_id, ts=ts, limit=1, inclusive=True)
                msgs = resp.get("messages", [])
                if msgs:
                    root_ts = msgs[0].get("ts", ts)
            except SlackApiError as e:
                logger.warning(f"[Complete] Failed to resolve root_ts: {e}")

            # Search SRE project for issues linked to this thread
            issues = self.jira.search_issues_by_slack_thread(channel_id, root_ts, project_key=self.PROJECT_KEY)
            logger.info(f"[Complete] Found {len(issues)} SRE issue(s) for thread ts={root_ts}")

            if not issues:
                client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="⚠️ 해당 스레드에서 확인된 SRE 티켓이 없습니다."
                )
                return

            # Transition each issue and its subtasks to done
            results = []
            for issue in issues:
                key = issue["key"]
                subtasks = self.jira.get_subtasks(key)
                logger.info(f"[Complete] {key} has {len(subtasks)} subtask(s)")

                # Transition subtasks first, then parent
                subtask_results = []
                for sub in subtasks:
                    sub_success = self.jira.transition_issue_to_done(sub["key"])
                    subtask_results.append({**sub, "success": sub_success})
                    logger.info(f"[Complete]   ↳ {sub['key']} transition to done: {'OK' if sub_success else 'FAIL'}")

                parent_success = self.jira.transition_issue_to_done(key)
                logger.info(f"[Complete] {key} transition to done: {'OK' if parent_success else 'FAIL'}")
                results.append({**issue, "success": parent_success, "subtasks": subtask_results})

            # Build and post result to thread
            lines = ["✅ 작업완료 하였습니다."]
            fail_list = []
            for r in results:
                status_icon = "✓" if r["success"] else "⚠️"
                lines.append(f"  {status_icon} *<{r['url']}|{r['key']}>* {r['summary']}")
                for sub in r.get("subtasks", []):
                    sub_icon = "✓" if sub["success"] else "⚠️"
                    lines.append(f"      ↳ {sub_icon} <{sub['url']}|{sub['key']}> {sub['summary']}")
                if not r["success"]:
                    fail_list.append(r)
                fail_list.extend([s for s in r.get("subtasks", []) if not s["success"]])

            if fail_list:
                lines.append("\n이미 완료 상태이거나 전환 불가 상태일 수 있습니다.")

            client.chat_postMessage(
                channel=channel_id,
                thread_ts=root_ts,
                text="\n".join(lines)
            )

        except Exception as e:
            logger.exception(f"[Complete] Pipeline failed: {e}")
            try:
                client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=f"⚠️ 처리 중 오류가 발생했습니다: {str(e)[:200]}"
                )
            except Exception:
                pass
