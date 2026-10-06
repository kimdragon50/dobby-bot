"""APScheduler-based daily briefing scheduler"""
import asyncio
import threading
from datetime import date, datetime, timedelta
import zoneinfo

from src._shared.config import Config
from src._shared.logger import logger
from src._shared.llm import LLMService
from src.command.calendar_briefing.user_store import UserStore
from src.command.calendar_briefing.services import CalendarService, BriefingService
from src.command.calendar_briefing.distributed_lock import DistributedLock
from src.command.calendar_briefing.prompts import (
    SYSTEM_PROMPT,
    HUMAN_PROMPT,
    NO_EVENTS_MESSAGE,
)

KST = zoneinfo.ZoneInfo("Asia/Seoul")


class BriefingScheduler:
    """매일 오후 8시에 등록된 사용자에게 내일 일정 브리핑 발송"""

    def __init__(self, slack_client, llm_service: LLMService, user_store: UserStore):
        self.client = slack_client
        self.llm = llm_service
        self.user_store = user_store
        self._scheduler = None
        self._lock = DistributedLock()

    def start(self):
        """스케줄러 시작"""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            self._scheduler = BackgroundScheduler(timezone=KST)
            self._scheduler.add_job(
                self._run_briefing,
                CronTrigger(hour=8, minute=10, timezone=KST),
                id="daily_briefing",
                name="Daily Calendar Briefing (08:10 KST)",
            )
            self._scheduler.start()
            logger.info("[Briefing] Scheduler started - daily at 08:10 KST")
        except ImportError:
            logger.warning(
                "[Briefing] apscheduler not installed, scheduler disabled. "
                "Install with: pip install apscheduler"
            )
        except Exception as e:
            logger.error(f"[Briefing] Failed to start scheduler: {e}")

    def stop(self):
        """스케줄러 중지"""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("[Briefing] Scheduler stopped")

    def _run_briefing(self):
        """모든 등록된 사용자에게 브리핑 발송 (분산 락으로 중복 실행 방지)"""
        lock_key = "daily_briefing"
        if not self._lock.acquire(lock_key):
            logger.info("[Briefing] Another pod is running briefing, skipping")
            return

        try:
            users = self.user_store.get_all_users()
            if not users:
                logger.info("[Briefing] No registered users, skipping")
                return

            # 오늘 일정 브리핑
            today = datetime.now(KST).date()
            logger.info(
                f"[Briefing] Running daily briefing for {len(users)} users "
                f"(target: {today})"
            )

            for user in users:
                try:
                    self._send_briefing_to_user(user, today)
                except Exception as e:
                    logger.error(
                        f"[Briefing] Failed for user {user.get('slack_user_id')}: {e}",
                        exc_info=True,
                    )
        finally:
            self._lock.release(lock_key)

    def send_test_briefing(self, slack_user_id: str, calendar_id: str):
        """테스트 브리핑 발송 (background thread)"""
        thread = threading.Thread(
            target=self._send_test,
            args=(slack_user_id, calendar_id),
        )
        thread.start()

    def _send_test(self, slack_user_id: str, calendar_id: str):
        """테스트 브리핑 실행"""
        tomorrow = datetime.now(KST).date() + timedelta(days=1)
        user = {
            "slack_user_id": slack_user_id,
            "calendar_id": calendar_id,
        }
        try:
            self._send_briefing_to_user(user, tomorrow)
        except Exception as e:
            logger.error(f"[Briefing] Test briefing failed: {e}", exc_info=True)
            try:
                self.client.chat_postMessage(
                    channel=slack_user_id,
                    text=f":warning: 브리핑 테스트 실패: {e}",
                )
            except Exception:
                pass

    def _send_briefing_to_user(self, user: dict, target_date: date):
        """특정 사용자에게 브리핑 발송"""
        slack_user_id = user["slack_user_id"]
        calendar_id = user["calendar_id"]

        # 1. 캘린더 이벤트 조회
        events = CalendarService.fetch_events(calendar_id, target_date)

        # 2. 브리핑 생성
        date_str = BriefingService.format_date_str(target_date)

        if not events:
            briefing_text = NO_EVENTS_MESSAGE
        else:
            events_text = BriefingService.format_events_for_prompt(events)
            input_data = {
                "date_str": date_str,
                "event_count": str(len(events)),
                "events_text": events_text,
            }

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                briefing_text = loop.run_until_complete(
                    self.llm.invoke(SYSTEM_PROMPT, HUMAN_PROMPT, input_data)
                )
            finally:
                loop.close()

        # 3. Slack DM 발송
        header_text = f":calendar: {date_str} 일정 브리핑"
        self.client.chat_postMessage(
            channel=slack_user_id,
            text=header_text,
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": header_text,
                        "emoji": True,
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": briefing_text},
                },
            ],
        )

        logger.info(
            f"[Briefing] Sent briefing to {slack_user_id} "
            f"({len(events)} events for {target_date})"
        )
