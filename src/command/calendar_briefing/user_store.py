"""DynamoDB-based user store for calendar briefing registrations"""
from datetime import datetime
from typing import Dict, List, Optional

from src._shared.dynamo import run_with_irsa
from src._shared.logger import logger

TABLE_NAME = "dobby-briefing-users"


def _run_with_irsa(fn):
    """Run fn(table) using IRSA credentials (공용 헬퍼 _shared/dynamo 사용)."""
    return run_with_irsa(TABLE_NAME, fn)


class UserStore:
    """DynamoDB-based user store"""

    def register(self, slack_user_id: str, slack_email: str, calendar_id: str) -> bool:
        """Register a user for calendar briefing"""
        try:
            _run_with_irsa(lambda t: t.put_item(Item={
                "slack_user_id": slack_user_id,
                "slack_email": slack_email,
                "calendar_id": calendar_id,
                "registered_at": datetime.now().isoformat(),
            }))
            logger.info(f"[Briefing] Registered user {slack_user_id} ({calendar_id})")
            return True
        except Exception as e:
            logger.error(f"[Briefing] Failed to register {slack_user_id}: {e}")
            return False

    def unregister(self, slack_user_id: str) -> bool:
        """Unregister a user"""
        try:
            _run_with_irsa(lambda t: t.delete_item(Key={"slack_user_id": slack_user_id}))
            logger.info(f"[Briefing] Unregistered user {slack_user_id}")
            return True
        except Exception as e:
            logger.error(f"[Briefing] Failed to unregister {slack_user_id}: {e}")
            return False

    def get_user(self, slack_user_id: str) -> Optional[Dict]:
        """Get a specific user's registration"""
        try:
            response = _run_with_irsa(lambda t: t.get_item(Key={"slack_user_id": slack_user_id}))
            return response.get("Item")
        except Exception as e:
            logger.error(f"[Briefing] Failed to get user {slack_user_id}: {e}")
            return None

    def get_all_users(self) -> List[Dict]:
        """Get all registered users"""
        try:
            response = _run_with_irsa(lambda t: t.scan())
            return response.get("Items", [])
        except Exception as e:
            logger.error(f"[Briefing] Failed to scan users: {e}")
            return []

    def is_registered(self, slack_user_id: str) -> bool:
        """Check if a user is registered"""
        return self.get_user(slack_user_id) is not None
