"""Calendar Briefing Handler - /dobby briefing

Manages user registration and daily calendar briefing.
Subcommands:
  /dobby briefing       → open registration modal
  /dobby briefing list  → show registered users
  /dobby briefing test  → send test briefing now
  /dobby briefing off   → unregister
"""
import json
from typing import Any, Dict

from slack_bolt import App

from src._shared.logger import logger
from src._shared.llm import LLMService
from src.command.calendar_briefing.user_store import UserStore
from src.command.calendar_briefing.services import CalendarService
from src.command.calendar_briefing.scheduler import BriefingScheduler


class CalendarBriefingHandler:
    """Handler for calendar briefing registration and management"""

    def __init__(self, app: App, llm_service: LLMService):
        self.app = app
        self.llm = llm_service
        self.user_store = UserStore()
        self.scheduler = None

    def register_handlers(self):
        """Register Slack modal and scheduler"""
        self.app.view("briefing_register_modal")(self.handle_modal_submission)
        logger.info("CalendarBriefingHandler: registered modal handler")

    def start_scheduler(self, client):
        """Start the daily briefing scheduler"""
        self.scheduler = BriefingScheduler(client, self.llm, self.user_store)
        self.scheduler.start()

    def stop_scheduler(self):
        """Stop the scheduler"""
        if self.scheduler:
            self.scheduler.stop()

    def handle_subcommand(self, command: dict, client):
        """Handle /dobby briefing subcommands"""
        text = command.get("text", "").strip()
        user_id = command["user_id"]
        channel_id = command["channel_id"]

        # Parse subcommand after "briefing"
        parts = text.split(maxsplit=1)
        sub = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            self._handle_list(client, channel_id, user_id)
        elif sub == "test":
            self._handle_test(client, channel_id, user_id)
        elif sub == "off":
            self._handle_unregister(client, channel_id, user_id)
        else:
            # Default: open registration modal
            self._open_register_modal(command, client)

    def _open_register_modal(self, command: dict, client):
        """Open the registration modal"""
        user_id = command["user_id"]
        existing = self.user_store.get_user(user_id)

        initial_value = existing["calendar_id"] if existing else None

        modal = {
            "type": "modal",
            "callback_id": "briefing_register_modal",
            "title": {"type": "plain_text", "text": "일정 브리핑 등록"},
            "submit": {
                "type": "plain_text",
                "text": "등록" if not existing else "수정",
            },
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":calendar: *매일 오후 8시에 내일 일정 브리핑*을 DM으로 보내드립니다.\n"
                            "Google Calendar ID (이메일)를 입력해주세요."
                        ),
                    },
                },
                {
                    "type": "input",
                    "block_id": "calendar_id_block",
                    "label": {"type": "plain_text", "text": "Google Calendar ID"},
                    "element": {
                        "type": "email_text_input",
                        "action_id": "calendar_id_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "your-email@example.com",
                        },
                        **(
                            {"initial_value": initial_value}
                            if initial_value
                            else {}
                        ),
                    },
                },
            ],
        }

        if existing:
            modal["blocks"].insert(
                0,
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":white_check_mark: 현재 등록됨: `{existing['calendar_id']}`",
                    },
                },
            )

        try:
            client.views_open(trigger_id=command["trigger_id"], view=modal)
        except Exception as e:
            logger.error(f"[Briefing] Failed to open modal: {e}")

    def handle_modal_submission(self, ack, view, client, body):
        """Handle registration modal submission"""
        ack()

        user_id = body["user"]["id"]
        values = view["state"]["values"]
        calendar_id = (
            values["calendar_id_block"]["calendar_id_input"]["value"].strip()
        )

        # Get user's email from Slack profile
        try:
            user_info = client.users_info(user=user_id)
            slack_email = user_info["user"]["profile"].get("email", "")
        except Exception:
            slack_email = ""

        # Register user
        if not self.user_store.register(user_id, slack_email, calendar_id):
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text=":x: 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                )
            except Exception:
                pass
            return

        # Get service account email for guide
        sa_email = CalendarService.get_service_account_email()

        # Send setup guide via DM
        guide_blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":white_check_mark: 일정 브리핑 등록 완료!",
                    "emoji": True,
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*등록된 캘린더:* `{calendar_id}`\n"
                        "*브리핑 시간:* 매일 오후 8시 (내일 일정)\n\n"
                    ),
                },
            },
        ]

        if sa_email:
            guide_blocks.extend([
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ":wrench: 설정이 필요합니다",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "브리핑을 받으려면 Google Calendar를 서비스 계정과 공유해야 합니다.\n\n"
                            "*설정 방법:*\n"
                            "1. <https://calendar.google.com|Google Calendar> 접속\n"
                            "2. 왼쪽 사이드바에서 캘린더 옆 :three_dots_vertical: 클릭 → *설정 및 공유*\n"
                            "3. *특정 사용자와 공유* 섹션에서 *사용자 추가*\n"
                            f"4. 아래 이메일을 추가하고 권한은 *일정의 모든 세부정보 보기*로 설정\n"
                        ),
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"```{sa_email}```",
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":bulb: 설정 완료 후 `/dobby briefing test`로 테스트해보세요!\n"
                            "등록 해제는 `/dobby briefing off`"
                        ),
                    },
                },
            ])
        else:
            guide_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":bulb: `/dobby briefing test`로 테스트해보세요!\n"
                        "등록 해제는 `/dobby briefing off`"
                    ),
                },
            })

        try:
            client.chat_postMessage(
                channel=user_id,
                text=":white_check_mark: 일정 브리핑 등록 완료!",
                blocks=guide_blocks,
            )
        except Exception as e:
            logger.error(f"[Briefing] Failed to send guide: {e}")

    def _handle_list(self, client, channel_id: str, user_id: str):
        """Show registered users list"""
        users = self.user_store.get_all_users()

        if not users:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":information_source: 등록된 사용자가 없습니다.",
            )
            return

        lines = [f":calendar: *일정 브리핑 등록 목록* ({len(users)}명)\n"]
        for u in users:
            lines.append(
                f"• <@{u['slack_user_id']}> - `{u['calendar_id']}` "
                f"(등록: {u.get('registered_at', 'N/A')[:10]})"
            )

        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="\n".join(lines),
        )

    def _handle_test(self, client, channel_id: str, user_id: str):
        """Send test briefing"""
        user = self.user_store.get_user(user_id)

        if not user:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=(
                    ":warning: 먼저 등록해주세요! "
                    "`/dobby briefing`으로 등록할 수 있습니다."
                ),
            )
            return

        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=":hourglass_flowing_sand: 테스트 브리핑을 생성 중입니다...",
        )

        if self.scheduler:
            self.scheduler.send_test_briefing(user_id, user["calendar_id"])
        else:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":warning: 스케줄러가 초기화되지 않았습니다.",
            )

    def _handle_unregister(self, client, channel_id: str, user_id: str):
        """Unregister user"""
        if self.user_store.unregister(user_id):
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":wave: 일정 브리핑 등록이 해제되었습니다.",
            )
        else:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":information_source: 등록되어 있지 않습니다.",
            )
