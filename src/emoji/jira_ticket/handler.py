"""Reaction Handler - Slack reaction to Jira ticket creation (Sync version)

Functionality: When a specific emoji reaction is added, create a Jira ticket from the thread content.
"""
import json
import re
import threading
from pathlib import Path
from typing import List, Dict, Optional, Set, Any
import yaml

from pydantic import ValidationError
from slack_sdk.errors import SlackApiError

from src._shared.logger import logger
from src._shared.config import Config
from src._shared.jira import JiraService
from src._shared.llm import LLMService, head_tail
from src.emoji.jira_ticket.prompts import SYSTEM_PROMPT, HUMAN_PROMPT, JiraPayload


# Load domain-specific configuration
def _load_config() -> Dict[str, Any]:
    """Load reaction handler configuration from local config.yaml"""
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


class ReactionConfig:
    """Reaction-based ticket creation configuration"""
    _config: Dict[str, Any] = None

    @classmethod
    def _ensure_loaded(cls):
        if cls._config is None:
            cls._config = _load_config()

    @classmethod
    def get_allowed_channels(cls) -> Set[str]:
        """Get allowed Slack channel IDs"""
        cls._ensure_loaded()
        channels = cls._config.get("allowed_channels", [])
        return set(channels) if channels else set()

    @classmethod
    def get_reaction_mapping(cls) -> Dict[str, Dict[str, str]]:
        """Get reaction to Jira project mapping from config.yaml"""
        cls._ensure_loaded()
        return cls._config.get("reaction_mapping", {})

    @classmethod
    def get_kpi_mode(cls) -> Dict[str, Any]:
        """Get KPI mode settings (keyword, initiative_keys, labels) from config.yaml"""
        cls._ensure_loaded()
        return cls._config.get("kpi_mode", {}) or {}

    @classmethod
    def get_default_labels(cls) -> List[str]:
        """Get initiative labels for normal (non-KPI) tickets"""
        cls._ensure_loaded()
        return list(cls._config.get("default_labels") or [])

    @classmethod
    def get_marker_labels(cls, reaction: str) -> List[str]:
        """Get the labels marking a ticket as auto-created by 도비

        Returns the fixed source label plus a trigger label for the reaction that
        created the ticket (e.g. source:dobby, trigger:op_cloud).
        """
        cls._ensure_loaded()
        labels = []
        source_label = cls._config.get("source_label")
        if source_label:
            labels.append(source_label)
        prefix = cls._config.get("trigger_label_prefix")
        if prefix and reaction:
            labels.append(f"{prefix}{reaction}")
        return labels

    @classmethod
    def get_default_story_points(cls) -> int:
        """Get default story points for emoji-triggered ticket creation"""
        cls._ensure_loaded()
        return cls._config.get("default_story_points", 1)


class ReactionHandler:
    """Handler for reaction-based Jira ticket creation (Sync version)

    Uses threading for background processing to avoid Slack timeout.
    """

    def __init__(self, jira_service: JiraService, llm_service: LLMService):
        self.jira = jira_service
        self.llm = llm_service

    def handle_reaction_added(self, event, client, say):
        """Handle reaction_added event (sync version)"""
        try:
            reaction = event.get("reaction")
            user_id = event.get("user")
            item = event.get("item", {})
            channel_id = item.get("channel")
            ts = item.get("ts")

            logger.info(f"[Reaction] Reaction received: reaction={reaction}, user={user_id}, channel={channel_id}")

            # Basic validation
            if not channel_id or not ts or not user_id:
                logger.info("[Reaction] Ignored event: missing channel, ts, or user")
                return

            # Channel filtering
            allowed_channels = ReactionConfig.get_allowed_channels()
            if allowed_channels and channel_id not in allowed_channels:
                logger.debug(f"[Reaction] Ignored event from unauthorized channel={channel_id}")
                return

            # Reaction filtering
            reaction_config = ReactionConfig.get_reaction_mapping()
            if reaction not in reaction_config:
                logger.debug(f"[Reaction] Ignored reaction: {reaction}")
                return

            project_key = reaction_config[reaction]["project_key"]
            parent_key = reaction_config[reaction].get("parent_key")
            issue_type = reaction_config[reaction].get("issue_type", "Story")
            logger.info(f"[Reaction] Triggered by {reaction} -> project={project_key}, parent={parent_key}, type={issue_type}")

            # Send immediate ephemeral ack with preview of the thread's first message
            try:
                ack_root_ts = ts
                try:
                    preview_resp = client.conversations_replies(
                        channel=channel_id, ts=ts, limit=1, inclusive=True
                    )
                    preview_msgs = preview_resp.get("messages", [])
                    if preview_msgs:
                        ack_root_ts = preview_msgs[0].get("ts", ts)
                except SlackApiError:
                    pass

                ack_thread_link = f"https://slack.com/archives/{channel_id}/p{ack_root_ts.replace('.', '')}?thread_ts={ack_root_ts}&cid={channel_id}"
                ack_text = f"🧙 도비가 Jira 티켓을 만들고 있어요. | <{ack_thread_link}|스레드 보기>"

                self._send_dm(client, user_id, ack_text)
            except SlackApiError as e:
                logger.warning(f"[Reaction] Failed to send ack message: {e}")

            # Create background thread for processing
            thread = threading.Thread(
                target=self._pipeline_handle_reaction,
                args=(client, channel_id, ts, project_key, user_id, parent_key, issue_type, reaction),
                name=f"reaction_{reaction}_{channel_id}_{ts}",
                daemon=True
            )
            thread.start()
            logger.info(f"[Reaction] Background thread started: {thread.name}")

        except Exception as e:
            logger.exception(f"[Reaction] Error in reaction_added handler: {e}")

    def _pipeline_handle_reaction(
        self, client, channel_id: str, thread_ts: str, project_key: str, user_id: str,
        parent_key: str = None, issue_type: str = "Story", reaction: str = ""
    ) -> None:
        """Pipeline: Thread collection -> LLM -> Jira -> Slack reply (runs in background thread)"""
        import asyncio

        current_step = "초기화"

        try:
            logger.info(f"[Reaction] Starting pipeline for channel={channel_id}, ts={thread_ts}")

            # 1) Collect thread messages (sync)
            current_step = "스레드 메시지 수집"
            logger.info("[Reaction] Step 1/3: Collecting thread messages...")
            thread_messages, requester_info, root_ts = self._get_thread_messages_with_requester(client, channel_id, thread_ts)
            requester_name = requester_info.get("name", "")
            requester_email = requester_info.get("email", "")

            # Resolve requester Jira accountId (for ADF mention) and display name
            requester_account_id = None
            if requester_email:
                req_jira = self.jira.get_user_by_email(requester_email)
                if req_jira:
                    requester_account_id = req_jira.get("accountId")
                    if not requester_name or requester_name.isdigit():
                        jira_display = req_jira.get("displayName")
                        if jira_display:
                            requester_name = jira_display
                            logger.info(f"[Reaction] Resolved requester name via Jira: {requester_name}")

            thread_text = "\n".join([m for m in thread_messages if m])
            logger.info(f"[Reaction] Collected {len(thread_messages)} messages ({len(thread_text)} chars), requester={requester_name}, email={requester_email}")

            if not thread_text:
                raise ValueError("스레드에 메시지가 없습니다")

            # Prepend requester info to thread text for LLM context
            if requester_name:
                thread_text = f"[요청자: {requester_name}]\n\n{thread_text}"

            # KPI mode: the "#KPI" keyword swaps the epic candidate pool to the epics of
            # the KPI initiatives, so the ticket lands under a KPI epic instead of the
            # reaction's default initiative.
            # 이니셔티브(KPI) -> 에픽(수행 항목) -> 태스크(이 티켓) -> 서브태스크(체크리스트)
            current_step = "KPI 에픽 조회"
            kpi_config = ReactionConfig.get_kpi_mode()
            kpi_keyword = kpi_config.get("keyword") or "#KPI"
            kpi_epics: List[Dict] = []
            if kpi_config and self._has_kpi_keyword(thread_text, kpi_keyword):
                initiative_keys = kpi_config.get("initiative_keys") or []
                logger.info(f"[Reaction] KPI keyword '{kpi_keyword}' detected -> initiatives={initiative_keys}")
                # The keyword is a routing directive, not content — keep it out of the ticket
                thread_text = self._strip_kpi_keyword(thread_text, kpi_keyword)
                kpi_epics = self.jira.get_epics_under_parents(initiative_keys, project_key)
                logger.info(f"[Reaction] Found {len(kpi_epics)} epics under KPI initiatives")
                if not kpi_epics:
                    logger.warning(f"[Reaction] KPI mode requested but no epics found under {initiative_keys}")

            # Fetch epics for LLM to select the most appropriate one
            # 1st: epics under the parent initiative
            # 2nd: all project epics (fallback when no epics under initiative)
            current_step = "에픽 조회"
            epics = kpi_epics
            if not epics:
                if parent_key:
                    logger.info(f"[Reaction] Fetching epics under parent {parent_key}...")
                    epics = self.jira.get_epics_under_parent(parent_key, project_key)
                    logger.info(f"[Reaction] Found {len(epics)} epics under {parent_key}")
                if not epics:
                    logger.info(f"[Reaction] No epics under parent, fetching all epics in project {project_key}...")
                    epics = self.jira.get_epics(project_key)
                    logger.info(f"[Reaction] Found {len(epics)} project-level epics")

            # 2) LLM processing (async -> sync via asyncio.run)
            current_step = "LLM 처리"
            logger.info("[Reaction] Step 2/3: Processing with LLM...")
            slack_thread_url = f"https://slack.com/archives/{channel_id}/p{root_ts.replace('.', '')}?thread_ts={root_ts}&cid={channel_id}"
            llm_result = asyncio.run(self._process_thread_to_jira(
                thread_text, slack_thread_url, epics, kpi_mode=bool(kpi_epics)
            ))
            logger.info("[Reaction] LLM processing completed")

            # Resolve reactor's Jira accountId before ticket creation (assignee is required)
            reactor_info = self._get_user_info(client, user_id)
            reactor_email = reactor_info.get("email", "")
            reactor_name = reactor_info.get("name", user_id)
            reactor_account_id = None
            if reactor_email:
                reactor_jira_user = self.jira.get_user_by_email(reactor_email)
                if reactor_jira_user and reactor_jira_user.get("accountId"):
                    reactor_account_id = reactor_jira_user["accountId"]
                    logger.info(f"[Reaction] Resolved reactor Jira accountId: {reactor_name} ({reactor_account_id})")
                else:
                    logger.warning(f"[Reaction] Could not resolve Jira user for reactor email: {reactor_email}")

            # The parent must be one of the epics we offered. A key the LLM invented, or
            # copied from somewhere else, would otherwise become the ticket's parent.
            selected_epic = self._find_epic(llm_result.get("epic_key"), epics)
            if llm_result.get("epic_key") and not selected_epic:
                logger.warning(
                    f"[Reaction] Discarding epic key outside the offered list: {llm_result.get('epic_key')}"
                )
                llm_result = {**llm_result, "epic_key": None}

            # In KPI mode the offered list IS the KPI epics, so a valid selection is a KPI epic
            kpi_epic = selected_epic if kpi_epics else None

            # Label policy lives in config.yaml: the initiative set depends on whether the
            # ticket landed under a KPI epic, and every auto-created ticket carries the
            # 도비 source + trigger markers.
            base_labels = (kpi_config.get("labels") or []) if kpi_epic else ReactionConfig.get_default_labels()
            ticket_labels = base_labels + ReactionConfig.get_marker_labels(reaction)
            logger.info(f"[Reaction] Ticket labels: {ticket_labels}")
            if kpi_epics and not kpi_epic:
                logger.warning(
                    f"[Reaction] KPI mode: no KPI epic selected (epic_key={llm_result.get('epic_key')}), "
                    f"falling back to parent {parent_key}"
                )
                llm_result = {**llm_result, "epic_key": None}
                self._post_to_slack_thread(
                    client, channel_id, thread_ts,
                    "ℹ️ 스레드 내용과 맞는 KPI 에픽을 찾지 못해 기본 이니셔티브 하위로 생성합니다."
                )

            # 3) Create main Jira task (async -> sync via asyncio.run)
            current_step = "Jira 태스크 생성"
            checklist = llm_result.get("checklist") or []
            task_story_points = llm_result.get("story_points") or ReactionConfig.get_default_story_points()

            # Resolve active sprint once (used for task + subtasks)
            active_sprint_id = None
            sprints = self.jira.get_sprints(project_key)
            active_sprints = [s for s in sprints if s.get("state") == "active"]
            if active_sprints:
                active_sprint_id = active_sprints[0]["id"]
                logger.info(f"[Reaction] Active sprint: {active_sprints[0]['name']} (id={active_sprint_id})")
            else:
                logger.info(f"[Reaction] No active sprint found for project {project_key}")

            logger.info(f"[Reaction] Step 3/4: Creating Jira task in {project_key} (parent={parent_key}, type={issue_type}, SP={task_story_points})...")

            # Pass empty checklist so steps aren't duplicated in description (subtasks created separately)
            payload_for_task = {**llm_result, "checklist": []}
            jira_ticket = asyncio.run(self.jira.create_ticket_from_llm(
                payload_for_task, project_key, requester_email, parent_key, issue_type, requester_name,
                assignee_account_id=reactor_account_id,
                story_points=task_story_points,
                requester_account_id=requester_account_id,
                required_labels=ticket_labels,
            ))
            key = jira_ticket.get("key", "UNKNOWN")
            url = jira_ticket["url"]
            summary = jira_ticket.get("summary")
            logger.info(f"[Reaction] Jira task created: {key} (SP={task_story_points}), assignee={reactor_name}")

            if active_sprint_id:
                if self.jira.move_issue_to_sprint(key, active_sprint_id):
                    logger.info(f"[Reaction] Task {key} moved to sprint {active_sprint_id}")
                else:
                    logger.warning(f"[Reaction] Failed to move task {key} to sprint {active_sprint_id}")

            # 4) Create subtasks from checklist items
            current_step = "서브태스크 생성"
            if not checklist:
                checklist = [{"title": summary, "steps": [], "story_points": task_story_points}]

            subtask_results = []
            logger.info(f"[Reaction] Step 4/4: Creating {len(checklist)} subtask(s) under {key}...")
            for item in checklist:
                subtask_summary = (item.get("title") or summary)[:255]
                subtask_sp = item.get("story_points") or task_story_points or ReactionConfig.get_default_story_points()
                steps = item.get("steps") or []
                steps_text = "\n".join([f"{i+1}. {s}" for i, s in enumerate(steps)]) if steps else ""
                # Append original thread content so the subtask keeps full technical details
                raw_content = thread_text[:4000] if thread_text else ""
                if raw_content:
                    subtask_desc = f"{steps_text}\n\nh2. 원본 요청 내용\n{raw_content}" if steps_text else f"h2. 원본 요청 내용\n{raw_content}"
                else:
                    subtask_desc = steps_text
                try:
                    subtask = asyncio.run(self.jira.create_subtask(
                        parent_key=key,
                        project_key=project_key,
                        summary=subtask_summary,
                        description=subtask_desc,
                        story_points=subtask_sp,
                        assignee_account_id=reactor_account_id,
                        labels=ticket_labels,
                    ))
                    if active_sprint_id:
                        self.jira.move_issue_to_sprint(subtask["key"], active_sprint_id)
                    subtask_results.append(subtask)
                    logger.info(f"[Reaction] Subtask created: {subtask['key']} (SP={subtask_sp})")
                except Exception as sub_e:
                    logger.error(f"[Reaction] Subtask creation failed for '{subtask_summary}': {sub_e}")
                    subtask_results.append({"key": "ERROR", "url": "", "summary": subtask_summary, "error": True})

            # Build result message
            # Report the full hierarchy (에픽 -> 태스크 -> 서브태스크) so every created issue
            # and its position is explicit, instead of leaving the task in the headline only.
            sp_text = f" ({task_story_points} SP)" if task_story_points else ""
            lines = ["✅ Jira 티켓이 생성되었습니다."]

            if selected_epic:
                epic_url = f"{Config.JIRA_SERVER_URL}/browse/{selected_epic['key']}"
                epic_label = "KPI 에픽" if kpi_epic else "에픽"
                lines.append(f"*{epic_label}:* <{epic_url}|{selected_epic['key']}> {selected_epic['summary']}")

            lines.append(f"*태스크:* *<{url}|{key}>*{sp_text} {summary}")

            if subtask_results:
                lines.append(f"*서브태스크 ({len(subtask_results)}개):*")
                for s in subtask_results:
                    if s.get("error"):
                        lines.append(f"  • ⚠️ {s['summary']} (생성 실패)")
                    else:
                        lines.append(f"  • <{s['url']}|{s['key']}> {s['summary']}")

            result_msg = "\n".join(lines)

            self._post_to_slack_thread(client, channel_id, thread_ts, result_msg)

            # Notify reactor via DM that processing is complete
            try:
                thread_link = f"https://slack.com/archives/{channel_id}/p{root_ts.replace('.', '')}?thread_ts={root_ts}&cid={channel_id}"
                short_summary = (summary or "")[:40] + ("..." if len(summary or "") > 40 else "")
                self._send_dm(client, user_id, f"✅ Jira 티켓을 생성완료 하였습니다. | *<{url}|{key}>* {short_summary} | <{thread_link}|스레드 보기>")
            except SlackApiError as e:
                logger.warning(f"[Reaction] Failed to send completion DM: {e}")

            logger.info(f"[Reaction] Pipeline completed for {key} with {len(subtask_results)} subtask(s)")

        except Exception as e:
            logger.exception(f"[Reaction] Pipeline failed at step: {current_step}")
            logger.error(f"[Reaction] Exception: {type(e).__name__}: {str(e)}")

            try:
                self._post_to_slack_thread(
                    client, channel_id, thread_ts,
                    f"⚠️ Jira 생성 중 오류가 발생했습니다 (단계: {current_step})\n```{type(e).__name__}: {str(e)[:200]}```"
                )
            except Exception as cleanup_error:
                logger.error(f"[Reaction] Failed to post error message: {cleanup_error}")

    async def _process_thread_to_jira(
        self, content: str, slack_thread_url: str = "", epics: List[Dict] = None,
        kpi_mode: bool = False
    ) -> Dict[str, Any]:
        """Process Slack thread content and extract structured Jira payload

        Args:
            content: Thread text content
            slack_thread_url: Slack thread URL
            epics: List of epics to choose from (optional)
            kpi_mode: True when epics are the KPI initiatives' epics, which makes epic
                selection mandatory rather than optional
        """
        try:
            text = (content or "").strip()
            if not text:
                raise ValueError("Empty thread content")

            # Truncate input if too long
            safe_input = head_tail(text, Config.INPUT_CHAR_LIMIT, Config.HEAD_TAIL_KEEP_RATIO)

            # Build epic section for prompt
            epic_section = ""
            if epics:
                epic_list = "\n".join([f"- {e['key']}: {e['summary']}" for e in epics])
                if kpi_mode:
                    epic_section = (
                        "KPI MODE IS ACTIVE. The epics below belong to the team's KPI "
                        "initiatives.\n"
                        "Set `epic_key` to the ONE epic whose scope best matches the thread. "
                        "Copy the key verbatim from this list — never invent a key. "
                        "Only use null if truly none of them is related.\n"
                        f"{epic_list}"
                    )
                else:
                    epic_section = (
                        "Available Epics (choose the most appropriate one based on thread content):\n"
                        f"{epic_list}"
                    )

            logger.info("Processing thread with LLM for Jira payload extraction")
            data = await self.llm.invoke_for_json(
                SYSTEM_PROMPT, HUMAN_PROMPT, {
                    "content": safe_input,
                    "slack_thread_url": slack_thread_url,
                    "epic_section": epic_section
                }
            )

            payload = JiraPayload.model_validate(data)
            return payload.model_dump()

        except (json.JSONDecodeError, ValidationError) as e:
            logger.error(f"Parse/validate error: {e}")
            first_line = (content or "").strip().splitlines()[0] if content else ""
            fallback = JiraPayload(
                summary=first_line[: Config.SUMMARY_MAX] or "Slack 스레드 정리",
                description=content[: Config.DESCRIPTION_MAX] if content else "",
                priority="Medium",
                labels=["slack", "auto-created"],
                checklist=[],
            )
            return fallback.model_dump()

        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            raise

    def _get_thread_messages_with_requester(self, client, channel_id: str, thread_ts: str) -> tuple[List[str], Dict[str, str], str]:
        """Get all messages from a thread with pagination and extract requester info (sync version)

        Returns:
            Tuple of (messages list, requester info dict with 'name' and 'email', root_ts)
        """
        try:
            messages: List[str] = []
            cursor = None
            page = 1
            user_cache: Dict[str, str] = {}  # Cache for user ID -> display name
            requester_info: Dict[str, str] = {"name": "", "email": ""}
            root_ts: str = thread_ts  # fallback; overwritten by actual root message ts

            while True:
                resp = client.conversations_replies(
                    channel=channel_id,
                    ts=thread_ts,
                    cursor=cursor,
                    limit=200,
                    inclusive=True,
                )
                raw_msgs = resp.get("messages", [])
                logger.debug(f"[Reaction] Page {page}: Retrieved {len(raw_msgs)} messages")

                for idx, msg in enumerate(raw_msgs):
                    # Extract requester from the first message of the thread
                    if page == 1 and idx == 0:
                        root_ts = msg.get("ts", thread_ts)  # actual thread root ts
                        first_user_id = msg.get("user")
                        if first_user_id:
                            requester_info = self._get_user_info(client, first_user_id)
                            # Also populate cache for mention replacement
                            user_cache[first_user_id] = requester_info.get("name") or ""

                    txt = (msg.get("text") or "").strip()
                    # Extract richer content from blocks or attachments (workflow messages)
                    blocks_txt = self._extract_text_from_blocks(msg.get("blocks", []))
                    if blocks_txt and len(blocks_txt) > len(txt):
                        txt = blocks_txt
                    attachments_txt = self._extract_text_from_attachments(msg.get("attachments", []))
                    if attachments_txt and len(attachments_txt) > len(txt):
                        txt = attachments_txt
                    if txt:
                        # Convert Slack mentions to display names
                        txt = self._replace_slack_mentions(client, txt, user_cache)
                        messages.append(txt)

                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
                page += 1

            return messages, requester_info, root_ts

        except SlackApiError as e:
            logger.error(f"[Reaction] Error getting thread content: {e}")
            return [], {"name": "", "email": ""}, thread_ts

    def _extract_text_from_blocks(self, blocks: List[Dict]) -> str:
        """Extract readable text from Slack Block Kit blocks (e.g. workflow messages)"""
        parts: List[str] = []
        for block in blocks:
            block_type = block.get("type")

            if block_type == "section":
                text_obj = block.get("text")
                if isinstance(text_obj, dict):
                    parts.append(text_obj.get("text", ""))
                fields = block.get("fields", [])
                for field in fields:
                    if isinstance(field, dict):
                        parts.append(field.get("text", ""))

            elif block_type == "rich_text":
                for element in block.get("elements", []):
                    elem_type = element.get("type")
                    if elem_type in ("rich_text_section", "rich_text_preformatted", "rich_text_quote"):
                        for sub in element.get("elements", []):
                            if sub.get("type") == "text":
                                parts.append(sub.get("text", ""))
                            elif sub.get("type") == "link":
                                parts.append(sub.get("url", ""))
                            elif sub.get("type") == "user":
                                parts.append(f"<@{sub.get('user_id', '')}>")
                    elif elem_type == "rich_text_list":
                        for item in element.get("elements", []):
                            item_texts = []
                            for sub in item.get("elements", []):
                                if sub.get("type") == "text":
                                    item_texts.append(sub.get("text", ""))
                            if item_texts:
                                parts.append(f"• {''.join(item_texts)}")

            elif block_type == "header":
                text_obj = block.get("text")
                if isinstance(text_obj, dict):
                    parts.append(text_obj.get("text", ""))

            elif block_type == "context":
                for elem in block.get("elements", []):
                    if isinstance(elem, dict) and elem.get("type") in ("mrkdwn", "plain_text"):
                        parts.append(elem.get("text", ""))

        return "\n".join(p for p in parts if p.strip()).strip()

    def _extract_text_from_attachments(self, attachments: List[Dict]) -> str:
        """Extract text from Slack attachments (e.g. Slack List / workflow request forms)"""
        parts: List[str] = []
        for att in attachments:
            # Slack List record (workflow request forms)
            list_record = att.get("list_record", {})
            if list_record:
                record = list_record.get("record", {})
                fields = record.get("fields", [])
                schema = list_record.get("schema", [])
                # Build schema lookup: column_id -> name
                schema_map = {s.get("id"): s.get("name") for s in schema}
                # Also build choices lookup for select fields
                choices_map: Dict[str, Dict[str, str]] = {}
                for s in schema:
                    if s.get("type") == "select":
                        choices = s.get("options", {}).get("choices", [])
                        choices_map[s.get("id")] = {c["value"]: c["label"] for c in choices}

                for field in fields:
                    column_id = field.get("column_id", "")
                    field_name = schema_map.get(column_id, "")
                    # Skip internal fields
                    if field.get("key") in ("todo_completed",):
                        continue
                    # Text fields with plain text
                    text_val = field.get("text", "")
                    if text_val:
                        if field_name:
                            parts.append(f"{field_name}: {text_val}")
                        else:
                            parts.append(text_val)
                    # Select fields: resolve value to label
                    elif field.get("select") and column_id in choices_map:
                        labels = [choices_map[column_id].get(v, v) for v in field["select"]]
                        if field_name:
                            parts.append(f"{field_name}: {', '.join(labels)}")
                continue

            # Standard attachments fallback
            fallback = att.get("text") or att.get("fallback", "")
            if fallback:
                title = att.get("title", "")
                if title:
                    parts.append(title)
                parts.append(fallback)

        return "\n".join(p for p in parts if p.strip()).strip()

    def _get_user_info(self, client, user_id: str) -> Dict[str, str]:
        """Get user info (name and email) from Slack API"""
        try:
            user_info = client.users_info(user=user_id)
            if user_info.get("ok"):
                user = user_info.get("user", {})
                profile = user.get("profile", {})
                display_name = (
                    user.get("real_name")
                    or profile.get("display_name")
                    or user.get("name")
                    or ""
                )
                # Filter out purely numeric IDs (e.g. "123456") — not a real name
                if display_name.isdigit():
                    display_name = ""
                email = profile.get("email", "")
                return {"name": display_name, "email": email}
        except SlackApiError as e:
            logger.warning(f"[Reaction] Failed to fetch user info for {user_id}: {e}")

        return {"name": "", "email": ""}

    def _get_user_display_name(self, client, user_id: str, user_cache: Dict[str, str]) -> str:
        """Get user display name from Slack API with caching"""
        if user_id in user_cache:
            return user_cache[user_id]

        try:
            user_info = client.users_info(user=user_id)
            if user_info.get("ok"):
                user = user_info.get("user", {})
                display_name = (
                    user.get("real_name")
                    or user.get("profile", {}).get("display_name")
                    or user.get("name")
                    or ""
                )
                if display_name.isdigit():
                    display_name = ""
                if display_name:
                    user_cache[user_id] = display_name
                    return display_name
        except SlackApiError as e:
            logger.warning(f"[Reaction] Failed to fetch user info for {user_id}: {e}")

        user_cache[user_id] = ""
        return ""

    def _replace_slack_mentions(self, client, text: str, user_cache: Dict[str, str]) -> str:
        """Replace Slack user mentions (<@U...> or <@123456>) with display names"""
        # Pattern to match Slack user mentions: <@U1234567890>, <@123456>, <@U1234567890|username>
        mention_pattern = re.compile(r"<@([A-Za-z0-9]+)(?:\|[^>]*)?>")

        def replace_mention(match):
            user_id = match.group(1)
            return self._get_user_display_name(client, user_id, user_cache)

        return mention_pattern.sub(replace_mention, text)

    @staticmethod
    def _kpi_keyword_pattern(keyword: str) -> "re.Pattern":
        """Compile a case-insensitive, word-bounded pattern for the KPI keyword"""
        return re.compile(re.escape(keyword) + r"\b", re.IGNORECASE)

    def _has_kpi_keyword(self, text: str, keyword: str) -> bool:
        """Check whether the thread requests KPI mode"""
        if not text or not keyword:
            return False
        return bool(self._kpi_keyword_pattern(keyword).search(text))

    def _strip_kpi_keyword(self, text: str, keyword: str) -> str:
        """Remove the KPI keyword so the routing directive does not leak into the ticket"""
        return self._kpi_keyword_pattern(keyword).sub("", text).strip()

    @staticmethod
    def _find_epic(epic_key: Optional[str], epics: List[Dict]) -> Optional[Dict]:
        """Match an LLM-selected epic key against the epics that were offered

        Returns the epic dict, or None when the list is empty or the key was not in it
        (guards against a hallucinated key becoming the ticket's parent).
        """
        if not epic_key or not epics:
            return None

        normalized = epic_key.strip().upper()
        for epic in epics:
            if epic["key"].upper() == normalized:
                return epic
        return None

    def _send_dm(self, client, user_id: str, text: str):
        """Send a DM to the user via the bot"""
        try:
            resp = client.conversations_open(users=[user_id])
            dm_channel = resp["channel"]["id"]
            client.chat_postMessage(channel=dm_channel, text=text)
        except SlackApiError as e:
            logger.warning(f"[Reaction] Failed to send DM to {user_id}: {e}")

    def _post_to_slack_thread(self, client, channel_id: str, thread_ts: str, message: str):
        """Post message to Slack thread (sync version)"""
        try:
            response = client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=message
            )
            return response.get("ts")
        except SlackApiError as e:
            logger.error(f"[Reaction] Error posting message: {e}")
            return None
