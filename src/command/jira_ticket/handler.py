"""Jira Modal Handler - Slack shortcut/command to Jira ticket with Modal UI

Functionality: Message shortcut or /dobby jira_create command opens a modal for creating Jira tickets.
"""
import asyncio
import json
import re
from typing import Dict, Any, Optional, List
from slack_bolt import App


def convert_markdown_links_to_jira(text: str) -> str:
    """Convert Markdown links [text](url) to Jira wiki format [text|url]"""
    # Pattern: [text](url) -> [text|url]
    pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    return re.sub(pattern, r'[\1|\2]', text)

from src._shared.config import Config
from src._shared.logger import logger
from src._shared.jira import JiraService
from src._shared.llm import LLMService
from src.command.jira_ticket.prompts import (
    DESCRIPTION_FORMAT_SYSTEM_PROMPT,
    DESCRIPTION_FORMAT_HUMAN_PROMPT,
)


class JiraModalHandler:
    """Handler for modal-based Jira ticket creation"""

    def __init__(self, app: App, jira_service: JiraService, llm_service: LLMService):
        self.app = app
        self.jira = jira_service
        self.llm = llm_service
        self.label_cache: Optional[List[str]] = None
        self.project_cache: Optional[List[Dict]] = None
        self.project_cache_timestamp: float = 0

    def register_handlers(self):
        """Register all Slack event handlers (except /dobby command which is handled by WeeklyReportHandler)"""
        self.app.shortcut("create_jira_ticket")(self.handle_create_ticket_shortcut)
        # Note: /jira_create is now handled via /dobby jira_create in WeeklyReportHandler
        self.app.view("create_ticket_modal")(self.handle_ticket_submission)
        self.app.action("project_select")(self.handle_project_change)
        self.app.action("initiative_select")(self.handle_initiative_change)
        self.app.options("initiative_select")(self.handle_initiative_options)
        self.app.options("epic_select")(self.handle_epic_options)
        self.app.options("labels_select")(self.handle_label_options)
        self.app.options("assignee_select")(self.handle_assignee_options)
        self.app.options("sprint_select")(self.handle_sprint_options)
        logger.info("JiraModalHandler: registered all handlers (including epic_select options)")

    def _get_cached_projects(self) -> List[Dict]:
        """Get projects from cache or fetch from Jira"""
        import time
        current_time = time.time()
        cache_expiry = 300  # 5 minutes

        if self.project_cache is not None and (current_time - self.project_cache_timestamp) < cache_expiry:
            return self.project_cache

        self.project_cache = self.jira.get_all_projects()
        self.project_cache_timestamp = current_time
        return self.project_cache

    def _build_modal_view(self, initial_text: str = "", initial_project: str = "") -> Dict[str, Any]:
        """Build the modal view for ticket creation"""
        projects = self._get_cached_projects()
        project_options = [
            {
                "text": {"type": "plain_text", "text": f"{p['key']} - {p['name']}"},
                "value": p["key"],
            }
            for p in projects[:100]
        ]

        default_project = initial_project or (projects[0]["key"] if projects else "PROJ")
        issue_types = self.jira.get_issue_types(default_project)
        issue_type_options = [
            {"text": {"type": "plain_text", "text": it["name"]}, "value": it["name"]}
            for it in issue_types[:100]
        ]

        # Load initiatives for the default project
        initiatives = self.jira.get_initiatives(default_project)
        initiative_options = [{"text": {"type": "plain_text", "text": "None"}, "value": "none"}]
        for init in initiatives[:100]:
            initiative_options.append({
                "text": {"type": "plain_text", "text": f"{init['key']}: {init['summary'][:70]}"},
                "value": init["key"],
            })

        project_accessory = {
            "type": "static_select",
            "action_id": "project_select",
            "placeholder": {"type": "plain_text", "text": "Select project"},
            "options": project_options or [{"text": {"type": "plain_text", "text": "No projects"}, "value": "none"}],
        }
        if project_options:
            project_accessory["initial_option"] = project_options[0]

        issue_type_element = {
            "type": "static_select",
            "action_id": "issue_type",
            "placeholder": {"type": "plain_text", "text": "Select issue type"},
            "options": issue_type_options or [{"text": {"type": "plain_text", "text": "Story"}, "value": "Story"}],
        }
        if issue_type_options:
            issue_type_element["initial_option"] = issue_type_options[0]

        model_name = Config.BEDROCK_MODEL_ID

        # Store project_key and initiative_key in private_metadata as JSON
        metadata = json.dumps({"project_key": default_project, "initiative_key": ""})

        modal_view = {
            "type": "modal",
            "callback_id": "create_ticket_modal",
            "title": {"type": "plain_text", "text": "Create Jira Ticket"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": metadata,
            "blocks": [
                {
                    "type": "section",
                    "block_id": "project_section",
                    "text": {"type": "mrkdwn", "text": "*Project* (required)"},
                    "accessory": project_accessory,
                },
                {
                    "type": "input",
                    "block_id": "issue_type_block",
                    "label": {"type": "plain_text", "text": "Issue Type"},
                    "element": issue_type_element,
                },
                {
                    "type": "input",
                    "block_id": "summary_block",
                    "label": {"type": "plain_text", "text": "Summary"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "summary",
                        "placeholder": {"type": "plain_text", "text": "Brief description of the issue"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "description_block",
                    "label": {"type": "plain_text", "text": "Description"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "description",
                        "multiline": True,
                        "initial_value": initial_text,
                        "placeholder": {"type": "plain_text", "text": "Detailed description"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "ai_processing_block",
                    "label": {"type": "plain_text", "text": "AI Options"},
                    "optional": True,
                    "element": {
                        "type": "checkboxes",
                        "action_id": "ai_processing",
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": f"AI로 Description 정리 ({model_name})"},
                                "value": "enable_ai",
                            }
                        ],
                    },
                },
                {
                    "type": "input",
                    "block_id": "initiative_block",
                    "dispatch_action": True,
                    "label": {"type": "plain_text", "text": "Initiative"},
                    "optional": True,
                    "element": {
                        "type": "static_select",
                        "action_id": "initiative_select",
                        "placeholder": {"type": "plain_text", "text": "Select initiative"},
                        "options": initiative_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "epic_block",
                    "label": {"type": "plain_text", "text": "Epic"},
                    "optional": True,
                    "element": {
                        "type": "external_select",
                        "action_id": "epic_select",
                        "placeholder": {"type": "plain_text", "text": "Select epic"},
                        "min_query_length": 0,
                    },
                },
                {
                    "type": "input",
                    "block_id": "sprint_block",
                    "label": {"type": "plain_text", "text": "Sprint"},
                    "optional": True,
                    "element": {
                        "type": "external_select",
                        "action_id": "sprint_select",
                        "placeholder": {"type": "plain_text", "text": "Select sprint"},
                        "min_query_length": 0,
                    },
                },
                {
                    "type": "input",
                    "block_id": "labels_block",
                    "label": {"type": "plain_text", "text": "Labels"},
                    "optional": True,
                    "element": {
                        "type": "multi_external_select",
                        "action_id": "labels_select",
                        "placeholder": {"type": "plain_text", "text": "Search labels"},
                        "min_query_length": 0,
                    },
                },
                {
                    "type": "input",
                    "block_id": "custom_labels_block",
                    "label": {"type": "plain_text", "text": "Custom Labels"},
                    "optional": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "custom_labels",
                        "placeholder": {"type": "plain_text", "text": "쉼표로 구분하여 입력 (예: label1, label2)"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "assignee_block",
                    "label": {"type": "plain_text", "text": "Assignee"},
                    "optional": True,
                    "element": {
                        "type": "external_select",
                        "action_id": "assignee_select",
                        "placeholder": {"type": "plain_text", "text": "Select assignee"},
                        "min_query_length": 0,
                    },
                },
                {
                    "type": "input",
                    "block_id": "due_date_block",
                    "label": {"type": "plain_text", "text": "Due Date"},
                    "optional": True,
                    "element": {
                        "type": "datepicker",
                        "action_id": "due_date",
                        "placeholder": {"type": "plain_text", "text": "Select due date"},
                    },
                },
            ],
        }

        return modal_view

    def handle_create_ticket_shortcut(self, ack, shortcut, client):
        """Handle message shortcut"""
        ack()
        try:
            initial_text = ""
            if "message" in shortcut:
                initial_text = shortcut["message"].get("text", "")

            modal_view = self._build_modal_view(initial_text=initial_text)
            client.views_open(trigger_id=shortcut["trigger_id"], view=modal_view)
            logger.info(f"[JiraModal] Opened modal for shortcut")

        except Exception as e:
            logger.error(f"[JiraModal] Failed to open modal: {e}")
            user_id = shortcut.get("user", {}).get("id")
            if user_id:
                try:
                    client.chat_postMessage(channel=user_id, text=f":x: Failed to open modal: {e}")
                except Exception:
                    pass

    def open_jira_modal(self, command, client):
        """Open Jira modal - called from /dobby jira command"""
        try:
            original_text = command.get("text", "")
            # Remove 'jira' prefix from text if present (user typed "/dobby jira" so text="jira")
            initial_text = original_text
            if initial_text.startswith("jira"):
                initial_text = initial_text[len("jira"):].strip()
            logger.info(f"[JiraModal] original_text='{original_text}', initial_text='{initial_text}'")
            modal_view = self._build_modal_view(initial_text=initial_text)
            client.views_open(trigger_id=command["trigger_id"], view=modal_view)
            logger.info(f"[JiraModal] Opened modal for /dobby jira")

        except Exception as e:
            logger.error(f"[JiraModal] Failed to open modal: {e}")
            user_id = command.get("user_id")
            if user_id:
                try:
                    client.chat_postMessage(channel=user_id, text=f":x: Failed to open modal: {e}")
                except Exception:
                    pass

    def handle_project_change(self, ack, body, client):
        """Handle project selection change"""
        ack()
        try:
            view = body["view"]
            view_id = view["id"]
            values = view["state"]["values"]
            selected_project = values["project_section"]["project_select"]["selected_option"]["value"]

            issue_types = self.jira.get_issue_types(selected_project)
            issue_type_options = [
                {"text": {"type": "plain_text", "text": it["name"]}, "value": it["name"]}
                for it in issue_types[:100]
            ]

            # Load initiatives for new project
            initiatives = self.jira.get_initiatives(selected_project)
            initiative_options = [{"text": {"type": "plain_text", "text": "None"}, "value": "none"}]
            for init in initiatives[:100]:
                initiative_options.append({
                    "text": {"type": "plain_text", "text": f"{init['key']}: {init['summary'][:70]}"},
                    "value": init["key"],
                })

            current_blocks = view["blocks"]
            for block in current_blocks:
                if block.get("block_id") == "issue_type_block":
                    if "element" in block and issue_type_options:
                        block["element"]["options"] = issue_type_options
                        block["element"]["initial_option"] = issue_type_options[0]
                elif block.get("block_id") == "initiative_block":
                    if "element" in block:
                        block["element"]["options"] = initiative_options
                        # Remove initial_option to reset selection when project changes
                        block["element"].pop("initial_option", None)

            # Update metadata with new project, reset initiative
            metadata = json.dumps({"project_key": selected_project, "initiative_key": ""})

            updated_view = {
                "type": "modal",
                "callback_id": view["callback_id"],
                "title": view["title"],
                "submit": view["submit"],
                "close": view["close"],
                "blocks": current_blocks,
                "private_metadata": metadata,
            }

            client.views_update(view_id=view_id, hash=view["hash"], view=updated_view)

        except Exception as e:
            logger.error(f"[JiraModal] Failed to update view: {e}")

    def handle_initiative_change(self, ack, body, client):
        """Handle initiative selection change - updates metadata for epic filtering"""
        logger.info("[JiraModal] handle_initiative_change called")
        ack()
        try:
            view = body["view"]
            view_id = view["id"]
            values = view["state"]["values"]

            # Parse current metadata
            try:
                metadata = json.loads(view.get("private_metadata", "{}"))
            except json.JSONDecodeError:
                metadata = {"project_key": view.get("private_metadata", ""), "initiative_key": ""}

            logger.info(f"[JiraModal] handle_initiative_change: current metadata={metadata}")

            # Get selected initiative
            initiative_select = values.get("initiative_block", {}).get("initiative_select", {})
            selected_option = initiative_select.get("selected_option")
            initiative_key = selected_option.get("value", "") if selected_option else ""
            if initiative_key == "none":
                initiative_key = ""

            logger.info(f"[JiraModal] handle_initiative_change: selected initiative_key={initiative_key}")

            # Update metadata with initiative_key
            metadata["initiative_key"] = initiative_key
            new_metadata = json.dumps(metadata)
            logger.info(f"[JiraModal] handle_initiative_change: new metadata={new_metadata}")

            updated_view = {
                "type": "modal",
                "callback_id": view["callback_id"],
                "title": view["title"],
                "submit": view["submit"],
                "close": view["close"],
                "blocks": view["blocks"],
                "private_metadata": new_metadata,
            }

            client.views_update(view_id=view_id, hash=view["hash"], view=updated_view)
            logger.info(f"[JiraModal] Initiative changed to: {initiative_key}")

        except Exception as e:
            logger.error(f"[JiraModal] Failed to handle initiative change: {e}")

    def handle_ticket_submission(self, ack, body, client, view):
        """Handle modal submission"""
        user_id = body["user"]["id"]
        logger.info(f"[JiraModal] Ticket submission from user {user_id}")

        values = view["state"]["values"]

        # Get project
        project_key = (
            values.get("project_section", {})
            .get("project_select", {})
            .get("selected_option", {})
            .get("value", "")
        )
        if not project_key:
            ack(response_action="errors", errors={"project_section": "Please select a project"})
            return

        issue_type = values["issue_type_block"]["issue_type"]["selected_option"]["value"]
        summary = values["summary_block"]["summary"]["value"]
        description = values["description_block"]["description"]["value"]

        # Validate required fields
        errors = {}
        if not summary or len(summary.strip()) == 0:
            errors["summary_block"] = "Summary is required"
        if not description or len(description.strip()) == 0:
            errors["description_block"] = "Description is required"

        if errors:
            ack(response_action="errors", errors=errors)
            return

        ack()

        # Check AI option
        ai_enabled = False
        if "ai_processing_block" in values:
            ai_options = values["ai_processing_block"]["ai_processing"].get("selected_options", [])
            ai_enabled = any(opt.get("value") == "enable_ai" for opt in ai_options)

        # Optional fields
        initiative_key = None
        if "initiative_block" in values and values["initiative_block"]["initiative_select"].get("selected_option"):
            initiative_value = values["initiative_block"]["initiative_select"]["selected_option"]["value"]
            if initiative_value != "none":
                initiative_key = initiative_value

        epic_key = None
        if "epic_block" in values and values["epic_block"]["epic_select"].get("selected_option"):
            epic_value = values["epic_block"]["epic_select"]["selected_option"]["value"]
            if epic_value != "none":
                epic_key = epic_value

        labels = []
        if "labels_block" in values and values["labels_block"]["labels_select"].get("selected_options"):
            labels = [opt["value"] for opt in values["labels_block"]["labels_select"]["selected_options"]]

        # Custom labels (comma-separated)
        if "custom_labels_block" in values:
            custom_labels_value = values["custom_labels_block"]["custom_labels"].get("value", "")
            if custom_labels_value:
                custom_labels = [label.strip() for label in custom_labels_value.split(",") if label.strip()]
                labels.extend(custom_labels)

        assignee_id = None
        if "assignee_block" in values and values["assignee_block"]["assignee_select"].get("selected_option"):
            assignee_value = values["assignee_block"]["assignee_select"]["selected_option"]["value"]
            if assignee_value != "none":
                assignee_id = assignee_value

        sprint_id = None
        if "sprint_block" in values and values["sprint_block"]["sprint_select"].get("selected_option"):
            sprint_value = values["sprint_block"]["sprint_select"]["selected_option"]["value"]
            if sprint_value != "none":
                try:
                    sprint_id = int(sprint_value)
                except ValueError:
                    pass

        due_date = None
        if "due_date_block" in values and values["due_date_block"]["due_date"].get("selected_date"):
            due_date = values["due_date_block"]["due_date"]["selected_date"]

        # Process with AI if enabled
        processed_description = description
        if ai_enabled:
            logger.info(f"[JiraModal] AI processing enabled for user {user_id}")
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text="🤖 AI가 내용을 정리하고 있습니다..."
                )
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                processed_description = loop.run_until_complete(
                    self.llm.invoke(
                        DESCRIPTION_FORMAT_SYSTEM_PROMPT,
                        DESCRIPTION_FORMAT_HUMAN_PROMPT,
                        {"description": description}
                    )
                )
                loop.close()
                logger.info(f"[JiraModal] AI processing completed")
            except Exception as llm_error:
                logger.error(f"[JiraModal] LLM error: {llm_error}")
                processed_description = description
                client.chat_postMessage(
                    channel=user_id,
                    text="⚠️ AI 처리 중 오류가 발생하여 원본 내용을 사용합니다."
                )

        # Convert any remaining Markdown links to Jira format
        processed_description = convert_markdown_links_to_jira(processed_description)

        # Create Jira issue
        # Note: Initiative is for UI filtering only. Task parent is always Epic.
        logger.info(f"[JiraModal] Creating issue: project={project_key}, type={issue_type}, epic={epic_key}")
        try:
            result = self.jira.create_issue(
                project_key=project_key,
                summary=summary,
                description=processed_description,
                issue_type=issue_type,
                epic_key=epic_key,
                labels=labels,
                assignee_id=assignee_id,
                due_date=due_date,
                sprint_id=sprint_id,
            )

            if result.get("success"):
                logger.info(f"[JiraModal] Ticket created: {result.get('key')}")
                message = f":white_check_mark: Jira ticket created!\n\n*Ticket:* <{result['url']}|{result['key']}>\n*Summary:* {summary}"
                client.chat_postMessage(channel=user_id, text=message)
            else:
                logger.error(f"[JiraModal] Failed to create ticket: {result.get('error')}")
                client.chat_postMessage(channel=user_id, text=f":x: Failed to create ticket: {result.get('error')}")

        except Exception as e:
            logger.error(f"[JiraModal] Exception during ticket creation: {e}")
            client.chat_postMessage(channel=user_id, text=f":x: Error: {str(e)}")

    def _parse_metadata(self, raw_metadata: str) -> Dict[str, str]:
        """Parse private_metadata JSON, with fallback for legacy format"""
        try:
            return json.loads(raw_metadata)
        except json.JSONDecodeError:
            # Legacy format: just project_key string
            return {"project_key": raw_metadata, "initiative_key": ""}

    def _get_project_key_from_view(self, view: Dict) -> str:
        """Extract project_key from metadata (updated by handle_project_change)"""
        # Use metadata as primary source (updated when project changes)
        raw_metadata = view.get("private_metadata", "")
        metadata = self._parse_metadata(raw_metadata)
        project_key = metadata.get("project_key", "")

        if project_key:
            logger.info(f"[JiraModal] _get_project_key: from metadata={project_key}")
            return project_key

        # Fallback to view state (for initial load before any project change)
        values = view.get("state", {}).get("values", {})
        project_select = values.get("project_section", {}).get("project_select", {})
        selected_option = project_select.get("selected_option")
        if selected_option:
            project_key = selected_option.get("value", "")
            logger.info(f"[JiraModal] _get_project_key: from state={project_key}")
            return project_key

        logger.warning("[JiraModal] _get_project_key: no project_key found")
        return ""

    def _get_initiative_key_from_view(self, view: Dict) -> str:
        """Extract initiative_key from view state first, then metadata"""
        # Try view state FIRST (most up-to-date when user just selected)
        values = view.get("state", {}).get("values", {})
        initiative_block = values.get("initiative_block", {})
        initiative_select = initiative_block.get("initiative_select", {})

        logger.info(f"[JiraModal] _get_initiative_key: initiative_block={initiative_block}")

        selected_option = initiative_select.get("selected_option")
        if selected_option:
            value = selected_option.get("value", "")
            if value and value != "none":
                logger.info(f"[JiraModal] _get_initiative_key: from state={value}")
                return value

        # Fallback to metadata
        raw_metadata = view.get("private_metadata", "")
        metadata = self._parse_metadata(raw_metadata)
        initiative_key = metadata.get("initiative_key", "")

        if initiative_key:
            logger.info(f"[JiraModal] _get_initiative_key: from metadata={initiative_key}")
            return initiative_key

        logger.info("[JiraModal] _get_initiative_key: not found")
        return ""

    def handle_epic_options(self, ack, payload):
        """Handle dynamic epic options - filters by initiative if selected"""
        logger.info(f"[JiraModal] handle_epic_options called")
        try:
            view = payload.get("view", {})
            logger.info(f"[JiraModal] view keys: {list(view.keys())}")

            # Log entire state values to debug
            state_values = view.get("state", {}).get("values", {})
            logger.info(f"[JiraModal] state values keys: {list(state_values.keys())}")
            for block_id, block_values in state_values.items():
                logger.info(f"[JiraModal] state[{block_id}]: {block_values}")

            project_key = self._get_project_key_from_view(view)
            initiative_key = self._get_initiative_key_from_view(view)

            logger.info(f"[JiraModal] Epic options: project={project_key}, initiative={initiative_key}")

            if not project_key:
                logger.warning("[JiraModal] No project_key found")
                ack(options=[{"text": {"type": "plain_text", "text": "Select project first"}, "value": "none"}])
                return

            # If initiative is selected, try to show epics under that initiative
            # Fall back to initiative's project epics if none found
            if initiative_key:
                logger.info(f"[JiraModal] Fetching epics under initiative {initiative_key}")
                epics = self.jira.get_epics_under_parent(initiative_key)
                logger.info(f"[JiraModal] Found {len(epics)} epics under initiative")
                if not epics:
                    # Fallback: use initiative's project key (e.g., PROJ-53 -> PROJ)
                    initiative_project = initiative_key.split('-')[0]
                    logger.info(f"[JiraModal] Falling back to project {initiative_project} epics")
                    epics = self.jira.get_epics(initiative_project)
                    logger.info(f"[JiraModal] Found {len(epics)} epics in project")
            else:
                logger.info(f"[JiraModal] Fetching epics for project {project_key}")
                epics = self.jira.get_epics(project_key)
                logger.info(f"[JiraModal] Found {len(epics)} epics")

            options = [{"text": {"type": "plain_text", "text": "None"}, "value": "none"}]
            for epic in epics[:100]:
                options.append({
                    "text": {"type": "plain_text", "text": f"{epic['key']}: {epic['summary'][:75]}"},
                    "value": epic["key"],
                })
            ack(options=options)
        except Exception as e:
            logger.error(f"[JiraModal] Error loading epics: {e}")
            ack(options=[{"text": {"type": "plain_text", "text": "Error loading epics"}, "value": "error"}])

    def handle_initiative_options(self, ack, payload):
        """Handle dynamic initiative options"""
        logger.info(f"[JiraModal] handle_initiative_options called")
        try:
            view = payload.get("view", {})
            project_key = self._get_project_key_from_view(view)

            logger.info(f"[JiraModal] Initiative options: project_key={project_key}")

            if not project_key:
                logger.warning("[JiraModal] No project_key for initiative options")
                ack(options=[{"text": {"type": "plain_text", "text": "Select project first"}, "value": "none"}])
                return

            initiatives = self.jira.get_initiatives(project_key)
            logger.info(f"[JiraModal] Found {len(initiatives)} initiatives")

            options = [{"text": {"type": "plain_text", "text": "None"}, "value": "none"}]
            for initiative in initiatives[:100]:
                options.append({
                    "text": {"type": "plain_text", "text": f"{initiative['key']}: {initiative['summary'][:75]}"},
                    "value": initiative["key"],
                })
            ack(options=options)
        except Exception as e:
            logger.error(f"[JiraModal] Error loading initiatives: {e}", exc_info=True)
            ack(options=[{"text": {"type": "plain_text", "text": "Error loading initiatives"}, "value": "error"}])

    def handle_label_options(self, ack, payload):
        """Handle dynamic label options"""
        try:
            search_value = (payload.get("value", "") or "").lower()

            if self.label_cache is None:
                self.label_cache = self.jira.get_all_labels()

            if search_value:
                filtered_labels = [l for l in self.label_cache if search_value in l.lower()][:100]
            else:
                filtered_labels = self.label_cache[:5]

            options = [{"text": {"type": "plain_text", "text": label}, "value": label} for label in filtered_labels]
            if not options:
                options = [{"text": {"type": "plain_text", "text": "No labels found"}, "value": "none"}]

            ack(options=options)
        except Exception as e:
            logger.error(f"[JiraModal] Error loading labels: {e}")
            ack(options=[{"text": {"type": "plain_text", "text": "Error loading labels"}, "value": "error"}])

    def handle_assignee_options(self, ack, payload):
        """Handle dynamic assignee options"""
        try:
            view = payload.get("view", {})
            project_key = self._get_project_key_from_view(view)

            if not project_key:
                ack(options=[{"text": {"type": "plain_text", "text": "Select project first"}, "value": "none"}])
                return

            search_value = (payload.get("value", "") or "").lower()
            users = self.jira.get_assignable_users(project_key)

            if search_value:
                filtered_users = [
                    u for u in users
                    if u["active"] and (search_value in u["display_name"].lower() or search_value in u.get("email", "").lower())
                ]
            else:
                filtered_users = [u for u in users if u["active"]]

            options = [{"text": {"type": "plain_text", "text": "Unassigned"}, "value": "none"}]
            for user in filtered_users[:100]:
                options.append({
                    "text": {"type": "plain_text", "text": user["display_name"]},
                    "value": user["account_id"],
                })

            ack(options=options)
        except Exception as e:
            logger.error(f"[JiraModal] Error loading assignees: {e}")
            ack(options=[{"text": {"type": "plain_text", "text": "Error loading assignees"}, "value": "error"}])

    def handle_sprint_options(self, ack, payload):
        """Handle dynamic sprint options"""
        try:
            view = payload.get("view", {})
            project_key = self._get_project_key_from_view(view)

            if not project_key:
                ack(options=[{"text": {"type": "plain_text", "text": "Select project first"}, "value": "none"}])
                return

            sprints = self.jira.get_sprints(project_key)
            active_sprints = [s for s in sprints if s["state"] != "closed"]

            options = [{"text": {"type": "plain_text", "text": "None"}, "value": "none"}]
            for sprint in active_sprints[:100]:
                state_indicator = "[Active] " if sprint["state"] == "active" else "[Future] " if sprint["state"] == "future" else ""
                options.append({
                    "text": {"type": "plain_text", "text": f"{state_indicator}{sprint['name']}"},
                    "value": str(sprint["id"]),
                })

            ack(options=options)
        except Exception as e:
            logger.error(f"[JiraModal] Error loading sprints: {e}")
            ack(options=[{"text": {"type": "plain_text", "text": "Error loading sprints"}, "value": "error"}])
