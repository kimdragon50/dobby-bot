"""Weekly Report Handler - Generates weekly Jira reports via /dobby wkreport

Functionality:
  - Opens a modal for selecting Jira group and date range
  - Generates weekly report summarized by epic using LLM
  - Adds classification labels to tickets (report:lead, report:team, report:pmo)
"""
import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

from slack_bolt import App

from src._shared.logger import logger
from src._shared.config import Config
from src._shared.jira import JiraService
from src._shared.llm import LLMService
from src.command.weekly_report.prompts import SYSTEM_PROMPT, HUMAN_PROMPT


class WeeklyReportHandler:
    """Handler for weekly report generation"""

    def __init__(self, app: App, jira_service: JiraService, llm_service: LLMService):
        self.app = app
        self.jira = jira_service
        self.llm = llm_service
        self._processed_views: Dict[str, float] = {}

    def register_handlers(self):
        """Register Slack event handlers"""
        self.app.view("weekly_report_modal")(self.handle_modal_submission)
        self.app.action("group_select")(self.handle_group_select)
        logger.info("WeeklyReportHandler: registered modal handlers")

    def handle_subcommand(self, command, client):
        """Handle /dobby wkreport subcommand - opens weekly report modal"""
        try:
            today = datetime.now()
            monday = today - timedelta(days=today.weekday())
            sunday = monday + timedelta(days=6)

            current_channel_id = command['channel_id']

            # Show loading modal first
            loading_view = {
                "type": "modal",
                "callback_id": "weekly_report_modal",
                "private_metadata": current_channel_id,
                "title": {"type": "plain_text", "text": "주간 리포트 생성"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "⏳ *Jira 그룹 목록을 불러오는 중...*"}
                    }
                ]
            }

            result = client.views_open(trigger_id=command['trigger_id'], view=loading_view)
            view_id = result['view']['id']

            # Update modal with groups in background
            def update_modal_with_groups():
                try:
                    groups = self.jira.get_groups(prefix=Config.JIRA_GROUP_PREFIX)
                    group_options = [
                        {"text": {"type": "plain_text", "text": group['name']}, "value": group['name']}
                        for group in groups
                    ]

                    if not group_options:
                        client.views_update(
                            view_id=view_id,
                            view={
                                "type": "modal",
                                "callback_id": "weekly_report_modal",
                                "title": {"type": "plain_text", "text": "오류"},
                                "close": {"type": "plain_text", "text": "닫기"},
                                "blocks": [
                                    {
                                        "type": "section",
                                        "text": {"type": "mrkdwn", "text": f"⚠️ '{Config.JIRA_GROUP_PREFIX}'로 시작하는 Jira 그룹을 찾을 수 없습니다."}
                                    }
                                ]
                            }
                        )
                        return

                    modal_view = {
                        "type": "modal",
                        "callback_id": "weekly_report_modal",
                        "private_metadata": current_channel_id,
                        "title": {"type": "plain_text", "text": "주간 리포트 생성"},
                        "submit": {"type": "plain_text", "text": "생성"},
                        "close": {"type": "plain_text", "text": "취소"},
                        "blocks": [
                            {
                                "type": "section",
                                "block_id": "group_block",
                                "text": {"type": "mrkdwn", "text": "*Jira 그룹 선택*"},
                                "accessory": {
                                    "type": "static_select",
                                    "action_id": "group_select",
                                    "placeholder": {"type": "plain_text", "text": "그룹을 선택하세요"},
                                    "options": group_options
                                }
                            },
                            {"type": "divider"},
                            {
                                "type": "input",
                                "block_id": "start_date_block",
                                "element": {
                                    "type": "datepicker",
                                    "action_id": "start_date",
                                    "initial_date": monday.strftime('%Y-%m-%d'),
                                    "placeholder": {"type": "plain_text", "text": "시작일 선택"}
                                },
                                "label": {"type": "plain_text", "text": "시작일"}
                            },
                            {
                                "type": "input",
                                "block_id": "end_date_block",
                                "element": {
                                    "type": "datepicker",
                                    "action_id": "end_date",
                                    "initial_date": sunday.strftime('%Y-%m-%d'),
                                    "placeholder": {"type": "plain_text", "text": "종료일 선택"}
                                },
                                "label": {"type": "plain_text", "text": "종료일"}
                            },
                            {
                                "type": "input",
                                "block_id": "channel_block",
                                "optional": True,
                                "element": {
                                    "type": "conversations_select",
                                    "action_id": "channel_select",
                                    "filter": {"include": ["public", "private"], "exclude_bot_users": True},
                                    "placeholder": {"type": "plain_text", "text": "채널을 선택하세요"}
                                },
                                "label": {"type": "plain_text", "text": "전송할 Slack 채널 (선택사항)"},
                                "hint": {"type": "plain_text", "text": "비워두면 현재 채널로 전송됩니다"}
                            }
                        ]
                    }

                    client.views_update(view_id=view_id, view=modal_view)
                except Exception as e:
                    logger.error(f"[WeeklyReport] Modal update failed: {e}")

            thread = threading.Thread(target=update_modal_with_groups)
            thread.start()

        except Exception as e:
            error_msg = str(e)
            if 'expired_trigger_id' in error_msg:
                client.chat_postMessage(
                    channel=command['channel_id'],
                    text="⚠️ 요청 처리가 지연되어 명령어가 만료되었습니다.\n잠시 후 `/dobby wkreport` 명령어를 다시 입력해주세요."
                )
            else:
                client.chat_postMessage(channel=command['channel_id'], text=f"❌ 오류 발생: {error_msg}")

    def handle_group_select(self, ack, body):
        """Handle group selection action (just acknowledge)"""
        ack()

    def handle_modal_submission(self, ack, view, client, body):
        """Handle modal submission"""
        ack()

        view_id = view.get('id', '')
        view_hash = view.get('hash', '')
        event_key = f"{view_id}:{view_hash}"
        current_time = time.time()

        # Clean old cache
        self._processed_views = {k: v for k, v in self._processed_views.items() if current_time - v < 60}

        if event_key in self._processed_views:
            logger.debug(f"[WeeklyReport] Duplicate event ignored: {event_key}")
            return

        self._processed_views[event_key] = current_time

        try:
            values = view['state']['values']

            # Get selected group
            group_block = values.get('group_block', {})
            group_select = group_block.get('group_select', {})
            selected_option = group_select.get('selected_option')
            selected_group = selected_option.get('value') if selected_option else None

            if not selected_group:
                raise ValueError("그룹이 선택되지 않았습니다")

            # Get dates
            start_date_str = values['start_date_block']['start_date']['selected_date']
            end_date_str = values['end_date_block']['end_date']['selected_date']

            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_date = end_date.replace(hour=23, minute=59, second=59)

            # Get target channel
            channel_select = values['channel_block']['channel_select'].get('selected_conversation')
            current_channel = view.get('private_metadata', '')
            target_channel = channel_select if channel_select else current_channel if current_channel else body['user']['id']

            # Post loading message
            response = client.chat_postMessage(
                channel=target_channel,
                text=f"📊 주간 리포트 생성 중... (그룹: {selected_group}, 기간: {start_date_str} ~ {end_date_str})"
            )

            # Get group members
            members = self.jira.get_group_members(selected_group)

            if not members:
                client.chat_update(
                    channel=target_channel,
                    ts=response['ts'],
                    text=f"⚠️ '{selected_group}' 그룹에 멤버가 없습니다."
                )
                return

            member_identifiers = [m['identifier'] for m in members]
            member_names = [m['displayName'] for m in members]

            # Generate report
            report, label_results, tickets = self._generate_weekly_report(member_identifiers, start_date, end_date)

            # Format member names
            member_names_str = ', '.join(member_names)
            if len(member_names_str) > 500:
                truncated_names = []
                current_length = 0
                for name in member_names:
                    if current_length + len(name) + 2 > 400:
                        break
                    truncated_names.append(name)
                    current_length += len(name) + 2
                member_names_str = ', '.join(truncated_names) + f' 외 {len(members) - len(truncated_names)}명'

            header = f"*그룹:* {selected_group}\n*멤버:* {member_names_str} ({len(members)}명)"
            main_ts = response['ts']

            # Update main message with header and thread guidance
            client.chat_update(
                channel=target_channel,
                ts=main_ts,
                text=f"{header}\n📊 주간 리포트가 생성되었습니다. 스레드를 확인해주세요."
            )

            # 1. Post ticket summary in thread (first)
            ticket_summary = self._format_ticket_summary(tickets, start_date_str, end_date_str)
            client.chat_postMessage(
                channel=target_channel,
                thread_ts=main_ts,
                text=ticket_summary
            )

            # 2. Post detailed report in thread (second)
            thread_message = f"```\n{report}\n```"
            byte_length = len(thread_message.encode('utf-8'))

            if byte_length <= 4000 and len(thread_message) <= 2500:
                client.chat_postMessage(
                    channel=target_channel,
                    thread_ts=main_ts,
                    text=thread_message
                )
            else:
                client.chat_postMessage(
                    channel=target_channel,
                    thread_ts=main_ts,
                    text="📎 리포트가 길어 파일로 첨부합니다."
                )

                full_report = f"그룹: {selected_group}\n멤버: {', '.join(member_names)} ({len(members)}명)\n\n{report}"
                client.files_upload_v2(
                    channel=target_channel,
                    thread_ts=main_ts,
                    content=full_report,
                    filename=f"weekly_report_{start_date_str}_{end_date_str}.txt",
                    title=f"주간 리포트 ({start_date_str} ~ {end_date_str})"
                )

            # 3. Post label results in thread (third)
            if label_results:
                label_message = self._format_label_results(label_results)
                client.chat_postMessage(
                    channel=target_channel,
                    thread_ts=main_ts,
                    text=label_message
                )

        except Exception as e:
            error_msg = f"❌ 리포트 생성 실패: {str(e)}"
            logger.error(error_msg)
            user_id = body['user']['id']
            client.chat_postMessage(channel=user_id, text=error_msg)

    def _generate_weekly_report(self, users: List[str], start_date: datetime, end_date: datetime) -> Tuple[str, Optional[Dict], List[Dict]]:
        """Generate weekly report using Jira and LLM services

        Returns:
            Tuple of (report_text, label_results, tickets)
        """
        logger.info(f"[WeeklyReport] Generating report for {len(users)} users")
        label_results = None

        # Get tickets from Jira
        tickets = self.jira.get_weekly_tickets(users, start_date, end_date)
        logger.info(f"[WeeklyReport] Found {len(tickets)} tickets")

        if not tickets:
            return "이번 주에 업데이트된 티켓이 없습니다.", None, []

        # Group by initiative and epic
        grouped_tickets = self.jira.group_tickets_by_initiative_and_epic(tickets)
        initiative_count = len(grouped_tickets)
        epic_count = sum(len(epics) for epics in grouped_tickets.values())
        logger.info(f"[WeeklyReport] Grouped into {initiative_count} initiatives, {epic_count} epics")

        # Compute Friday of the report week as duedate fallback
        days_to_friday = (4 - start_date.weekday()) % 7
        report_friday = (start_date + timedelta(days=days_to_friday)).strftime("%m/%d")

        # Prepare ticket data for LLM
        tickets_text = self._format_tickets_for_llm(grouped_tickets, report_friday)

        # Generate report with LLM
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            llm_response = loop.run_until_complete(
                self.llm.invoke(
                    SYSTEM_PROMPT,
                    HUMAN_PROMPT,
                    {
                        "tickets_text": tickets_text,
                        "jira_base_url": Config.JIRA_SERVER_URL.rstrip("/"),
                    },
                )
            )

            # Parse report and classification from LLM response
            report, classification = self._parse_llm_response(llm_response)

            # Add labels to tickets based on classification
            # Pass tickets to ensure all tickets and subtasks are labeled
            label_results = self._apply_ticket_labels(classification, tickets)

        except Exception as e:
            logger.error(f"[WeeklyReport] LLM invocation failed: {e}")
            report = self._generate_fallback_report(grouped_tickets)
            # Still apply labels even if LLM fails - all tickets go to report:team
            label_results = self._apply_ticket_labels(None, tickets)
        finally:
            loop.close()

        # Add header
        header = f"📅 주간 업무 리포트 ({start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')})\n\n"

        return header + report, label_results, tickets

    def _parse_llm_response(self, response: str) -> Tuple[str, Optional[Dict[str, List[str]]]]:
        """Parse LLM response to extract report text and ticket classification JSON

        Returns:
            Tuple of (report_text, classification_dict)
        """
        classification = None

        # Find JSON block in response (```json ... ``` or just { ... })
        json_pattern = r'```json\s*(\{[\s\S]*?\})\s*```'
        match = re.search(json_pattern, response)

        if match:
            try:
                classification = json.loads(match.group(1))
                # Remove JSON block from report
                report = re.sub(json_pattern, '', response).strip()
                logger.info(f"[WeeklyReport] Parsed classification: {classification}")
            except json.JSONDecodeError as e:
                logger.warning(f"[WeeklyReport] Failed to parse classification JSON: {e}")
                report = response
        else:
            # Try to find standalone JSON object at the end
            standalone_pattern = r'\n(\{[^{}]*"report:[^{}]*\})\s*$'
            match = re.search(standalone_pattern, response)
            if match:
                try:
                    classification = json.loads(match.group(1))
                    report = response[:match.start()].strip()
                    logger.info(f"[WeeklyReport] Parsed standalone classification: {classification}")
                except json.JSONDecodeError:
                    report = response
            else:
                report = response

        return report, classification

    def _apply_ticket_labels(self, classification: Optional[Dict[str, List[str]]], tickets: List[Dict]) -> Optional[Dict[str, any]]:
        """Apply classification labels to tickets in Jira

        Args:
            classification: LLM classification dict or None
            tickets: List of ticket dicts (includes subtasks in each ticket)

        Returns:
            Dict with label results or None if no labels to apply
        """
        if not tickets:
            return None

        all_ticket_keys = [t["key"] for t in tickets]

        # Filter to only valid report labels
        valid_labels = {"report:lead", "report:team", "report:pmo"}
        filtered_classification = {}

        if classification:
            filtered_classification = {
                label: list(keys) for label, keys in classification.items()
                if label in valid_labels and keys
            }

        # Find tickets not classified by LLM and auto-assign to report:team
        classified_keys = set()
        for keys in filtered_classification.values():
            classified_keys.update(keys)

        unclassified_keys = [key for key in all_ticket_keys if key not in classified_keys]

        if unclassified_keys:
            if "report:team" not in filtered_classification:
                filtered_classification["report:team"] = []
            filtered_classification["report:team"].extend(unclassified_keys)
            logger.info(f"[WeeklyReport] Auto-classified {len(unclassified_keys)} tickets to report:team")

        # Build parent key to label mapping
        parent_to_label = {}
        for label, keys in filtered_classification.items():
            for key in keys:
                parent_to_label[key] = label

        # Add subtasks with same label as their parent
        subtask_count = 0
        for ticket in tickets:
            parent_key = ticket["key"]
            parent_label = parent_to_label.get(parent_key)
            if parent_label:
                subtasks = ticket.get("subtasks", [])
                for subtask in subtasks:
                    subtask_key = subtask.get("key")
                    if subtask_key and subtask_key not in parent_to_label:
                        filtered_classification[parent_label].append(subtask_key)
                        subtask_count += 1

        if subtask_count > 0:
            logger.info(f"[WeeklyReport] Added {subtask_count} subtasks with parent labels")

        if not filtered_classification:
            logger.info("[WeeklyReport] No valid classification labels to apply")
            return None

        total_tickets = sum(len(keys) for keys in filtered_classification.values())
        logger.info(f"[WeeklyReport] Applying labels to {total_tickets} tickets (including subtasks)")

        try:
            results = self.jira.add_labels_to_tickets(filtered_classification)
            results["classification"] = filtered_classification
            logger.info(f"[WeeklyReport] Label results: {results['success']} success, {results['failed']} failed")
            return results
        except Exception as e:
            logger.error(f"[WeeklyReport] Failed to apply labels: {e}")
            return None

    def _format_label_results(self, results: Dict) -> str:
        """Format label results for Slack message with Jira links"""
        classification = results.get("classification", {})
        details = results.get("details", [])
        success = results.get("success", 0)
        failed = results.get("failed", 0)
        jira_url = Config.JIRA_SERVER_URL.rstrip("/")

        lines = ["*🏷️ Jira 티켓 라벨 자동 분류 완료*\n"]

        # Format each label category with ticket details
        label_names = {
            "report:lead": "📊 임원 보고",
            "report:team": "👥 팀 공유",
            "report:pmo": "🔄 팀간 이슈/의사결정"
        }

        for label, display_name in label_names.items():
            label_tickets = classification.get(label, [])
            if label_tickets:
                ticket_links = [f"<{jira_url}/browse/{key}|{key}>" for key in label_tickets]
                lines.append(f"*{display_name}* (`{label}`) - {len(label_tickets)}건")
                lines.append(f"  {', '.join(ticket_links)}")
                lines.append("")

        # Summary
        if failed > 0:
            lines.append(f"*결과:* ✅ 성공 {success}건 | ❌ 실패 {failed}건")
        else:
            lines.append(f"*결과:* ✅ 총 {success}건 라벨 적용 완료")

        return "\n".join(lines)

    def _format_ticket_summary(self, tickets: List[Dict], start_date: str, end_date: str) -> str:
        """Format ticket summary with total count and per-assignee breakdown

        Args:
            tickets: List of ticket dicts
            start_date: Start date string (YYYY-MM-DD)
            end_date: End date string (YYYY-MM-DD)

        Returns:
            Formatted summary string for Slack
        """
        jira_url = Config.JIRA_SERVER_URL.rstrip("/")
        total_count = len(tickets)

        lines = [
            f"*📋 티켓 현황 ({start_date} ~ {end_date})*",
            f"총 {total_count}건의 티켓이 조회되었습니다.\n"
        ]

        # Group tickets by assignee
        by_assignee: Dict[str, List[Dict]] = {}
        for ticket in tickets:
            assignee = ticket.get("assignee") or "미할당"
            if assignee not in by_assignee:
                by_assignee[assignee] = []
            by_assignee[assignee].append(ticket)

        # Sort by ticket count (descending)
        sorted_assignees = sorted(by_assignee.items(), key=lambda x: len(x[1]), reverse=True)

        for assignee, assignee_tickets in sorted_assignees:
            ticket_links = [f"<{jira_url}/browse/{t['key']}|{t['key']}>" for t in assignee_tickets]
            lines.append(f"*{assignee}* ({len(assignee_tickets)}건)")
            lines.append(f"  {', '.join(ticket_links)}")
            lines.append("")

        return "\n".join(lines)

    def _format_tickets_for_llm(self, grouped_tickets: Dict[str, Dict[str, List[Dict]]], report_friday: str = "") -> str:
        """Format grouped tickets as text for LLM input

        Args:
            grouped_tickets: Dict structure {initiative_name: {epic_name: [tickets]}}
            report_friday: Fallback due date (MM/DD) — Friday of the report week
        """
        ticket_info_parts = []

        for initiative_name, epics in grouped_tickets.items():
            initiative_parts = []

            # Add initiative header if it's not the fallback group
            if initiative_name != "기타 (Initiative 없음)":
                initiative_parts.append(f"[Initiative: {initiative_name}]")

            for epic_name, tickets in epics.items():
                ticket_lines = []
                for t in tickets:
                    info = f"- {t['key']}: {t['summary']} (상태: {t['status']})"
                    if t.get("description"):
                        desc = t["description"][:300]
                        info += f"\n  설명: {desc}"
                    if t.get("priority"):
                        info += f"\n  우선순위: {t['priority']}"
                    if t.get("labels"):
                        info += f"\n  라벨: {', '.join(t['labels'])}"
                    duedate = t.get("duedate")
                    if duedate:
                        # Convert YYYY-MM-DD to MM/DD
                        try:
                            from datetime import datetime as dt
                            info += f"\n  마감일: {dt.strptime(duedate, '%Y-%m-%d').strftime('%m/%d')}"
                        except ValueError:
                            info += f"\n  마감일: {duedate}"
                    elif report_friday:
                        info += f"\n  마감일: {report_friday} (기본값-해당주금요일)"

                    # Add subtasks if present
                    subtasks = t.get("subtasks", [])
                    if subtasks:
                        info += "\n  서브태스크:"
                        for st in subtasks:
                            status_info = st.get("resolution_date") or st.get("status", "진행 중")
                            st_line = f"\n    - {st['summary']} ({status_info})"
                            # Include subtask description for more context
                            st_desc = st.get("description", "")
                            if st_desc:
                                st_desc_truncated = st_desc[:200].replace("\n", " ")
                                st_line += f"\n      내용: {st_desc_truncated}"
                            info += st_line
                        logger.debug(f"[WeeklyReport] Ticket {t['key']} has {len(subtasks)} subtasks")

                    ticket_lines.append(info)

                epic_header = f"  [에픽: {epic_name}]" if initiative_name != "기타 (Initiative 없음)" else f"[에픽: {epic_name}]"
                initiative_parts.append(epic_header + "\n" + "\n".join(ticket_lines))

            ticket_info_parts.append("\n".join(initiative_parts))

        return "\n\n".join(ticket_info_parts)

    def _generate_fallback_report(self, grouped_tickets: Dict[str, Dict[str, List[Dict]]]) -> str:
        """Generate basic report when LLM fails

        Args:
            grouped_tickets: Dict structure {initiative_name: {epic_name: [tickets]}}
        """
        basic_report = []
        for initiative_name, epics in grouped_tickets.items():
            initiative_parts = []
            if initiative_name != "기타 (Initiative 없음)":
                initiative_parts.append(f"[Initiative: {initiative_name}]")

            for epic_name, tickets in epics.items():
                ticket_list = "\n".join(
                    [f"    - {t['key']}: {t['summary']}" for t in tickets]
                )
                epic_header = f"  [{epic_name}]" if initiative_name != "기타 (Initiative 없음)" else f"[{epic_name}]"
                initiative_parts.append(f"{epic_header}\n{ticket_list}")

            basic_report.append("\n".join(initiative_parts))
        return "\n\n".join(basic_report)
