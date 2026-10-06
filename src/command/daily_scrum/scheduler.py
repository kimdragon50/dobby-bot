"""데일리 스크럼 알림 스케줄러

평일(월~금) 지정 시각에, DynamoDB 에 등록된 모든 채널로 스크럼 알림을 게시한다.
채널 목록은 실행 시점에 조회하므로 등록/해제가 재배포 없이 즉시 반영된다.

여러 파드가 떠도 한 번만 게시되도록 날짜+채널 단위 분산 락을 쓴다.
"""
from datetime import datetime, timezone, timedelta

from src._shared.config import Config
from src._shared.lock import DistributedLock
from src._shared.logger import logger
from src.command.daily_scrum.channel_store import ScrumChannelStore

KST = timezone(timedelta(hours=9))


class ScrumScheduler:
    """평일 SCRUM_HOUR:SCRUM_MINUTE (KST) 에 등록 채널로 스크럼 알림 게시"""

    def __init__(self, slack_client, handler):
        self.client = slack_client
        self.handler = handler
        self.channels = ScrumChannelStore()
        self._scheduler = None
        self._lock = DistributedLock()

    def start(self):
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            hour, minute = Config.SCRUM_HOUR, Config.SCRUM_MINUTE
            self._scheduler = BackgroundScheduler(timezone=KST)
            self._scheduler.add_job(
                self._run,
                CronTrigger(
                    day_of_week=Config.SCRUM_DAYS, hour=hour, minute=minute, timezone=KST
                ),
                id="daily_scrum",
                name=f"Daily Scrum ({hour:02d}:{minute:02d} KST)",
            )
            self._scheduler.start()
            logger.info(
                f"[Scrum] Scheduler started — {Config.SCRUM_DAYS} {hour:02d}:{minute:02d} KST "
                "(대상 채널은 실행 시점에 조회)"
            )
        except ImportError:
            logger.warning("[Scrum] apscheduler 미설치 — 자동 게시 비활성")
        except Exception as e:
            logger.error(f"[Scrum] 스케줄러 시작 실패: {e}")

    def stop(self):
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("[Scrum] Scheduler stopped")

    def _run(self):
        channels = self.channels.list_all()
        if not channels:
            logger.info("[Scrum] 등록된 채널 없음 — 게시 건너뜀")
            return

        today = datetime.now(KST).strftime("%Y-%m-%d")
        posted = 0
        for item in channels:
            channel_id = item.get("channel_id", "")
            if not channel_id:
                continue
            # 채널별 락 — 한 채널이 실패해도 다른 채널은 진행된다
            if not self._lock.acquire(f"daily_scrum:{today}:{channel_id}"):
                logger.info(f"[Scrum] {channel_id} 는 다른 파드가 게시함 — 건너뜀")
                continue
            try:
                self.handler.post_announcement(self.client, channel_id, mention_channel=True)
                posted += 1
            except Exception as e:
                # 봇이 채널에서 빠졌거나 권한이 없을 수 있다 — 나머지 채널은 계속 진행
                logger.error(f"[Scrum] {channel_id} 게시 실패: {e}")
        logger.info(f"[Scrum] 자동 게시 완료: {posted}/{len(channels)} 채널")
