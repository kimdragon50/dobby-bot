"""스크럼 자동 게시 채널 저장소 (DynamoDB)

환경변수 대신 DynamoDB 에 두는 이유: 채널을 추가·제거할 때마다 재배포하지
않고 Slack 에서 바로 등록/해제할 수 있어야 한다.

테이블: dobby-scrum-channels
  PK channel_id(S)
  attrs: channel_name, registered_by, registered_at
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List

from src._shared.config import Config
from src._shared.dynamo import run_with_irsa
from src._shared.logger import logger

KST = timezone(timedelta(hours=9))


class ScrumChannelStore:
    """스크럼 알림을 받을 채널 목록"""

    def __init__(self, table_name: str = ""):
        self.table_name = table_name or Config.SCRUM_CHANNEL_TABLE

    def register(self, channel_id: str, channel_name: str, user_id: str) -> bool:
        try:
            run_with_irsa(self.table_name, lambda t: t.put_item(Item={
                "channel_id": channel_id,
                "channel_name": channel_name,
                "registered_by": user_id,
                "registered_at": datetime.now(KST).isoformat(),
            }))
            logger.info(f"[Scrum] 채널 등록: {channel_id} ({channel_name}) by {user_id}")
            return True
        except Exception as e:
            logger.error(f"[Scrum] 채널 등록 실패 {channel_id}: {e}")
            return False

    def unregister(self, channel_id: str) -> bool:
        try:
            run_with_irsa(self.table_name, lambda t: t.delete_item(
                Key={"channel_id": channel_id}
            ))
            logger.info(f"[Scrum] 채널 해제: {channel_id}")
            return True
        except Exception as e:
            logger.error(f"[Scrum] 채널 해제 실패 {channel_id}: {e}")
            return False

    def is_registered(self, channel_id: str) -> bool:
        return self.get(channel_id) is not None

    def get(self, channel_id: str):
        try:
            resp = run_with_irsa(self.table_name, lambda t: t.get_item(
                Key={"channel_id": channel_id}
            ))
            return resp.get("Item")
        except Exception as e:
            logger.error(f"[Scrum] 채널 조회 실패 {channel_id}: {e}")
            return None

    def list_all(self) -> List[Dict]:
        """등록된 모든 채널. 조회 실패 시 빈 목록 — 스케줄러가 죽지 않게."""
        try:
            resp = run_with_irsa(self.table_name, lambda t: t.scan())
            return resp.get("Items", [])
        except Exception as e:
            logger.error(f"[Scrum] 채널 목록 조회 실패: {e}")
            return []
