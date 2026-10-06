"""DynamoDB 조건부 쓰기 기반 분산 락 (스케줄러 리더 선출)

여러 Pod 에서 같은 스케줄 작업이 중복 실행되는 것을 막는다.
TTL lease 방식이라 Pod 가 죽어도 만료 후 자동 해제된다.

DynamoDB 접근은 _shared/dynamo.py 의 IRSA 헬퍼를 쓴다 (Bedrock 정적 키가
IRSA 를 가리는 문제 대응 — 자세한 내용은 해당 모듈 참조).
"""
import os
import time

from botocore.exceptions import ClientError

from src._shared.dynamo import run_with_irsa
from src._shared.logger import logger

TABLE_NAME = "dobby-scheduler-locks"
DEFAULT_TTL_SECONDS = 300  # 5분


class DistributedLock:
    """스케줄러용 분산 락.

    DynamoDB 장애 시에는 fail-open 한다 — 락을 못 잡았다고 스케줄 작업을
    통째로 건너뛰면 그날 알림이 아예 안 나가는 게 더 큰 문제다.
    (최악의 경우 중복 게시이며, 이는 사용자가 인지하고 넘길 수 있다)
    """

    def __init__(self):
        self._owner = f"{os.getenv('HOSTNAME', 'unknown')}-{os.getpid()}"

    def acquire(self, lock_key: str, ttl: int = DEFAULT_TTL_SECONDS) -> bool:
        """락 획득 시도. True 면 이 Pod 가 실행 담당."""
        now = int(time.time())
        try:
            run_with_irsa(TABLE_NAME, lambda t: t.put_item(
                Item={
                    "lock_key": lock_key,
                    "owner": self._owner,
                    "acquired_at": now,
                    "expires_at": now + ttl,
                },
                ConditionExpression=(
                    "attribute_not_exists(lock_key) OR expires_at < :now"
                ),
                ExpressionAttributeValues={":now": now},
            ))
            logger.info(f"[Lock] Acquired '{lock_key}' (owner={self._owner}, ttl={ttl}s)")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.debug(f"[Lock] '{lock_key}' held by another pod, skipping")
                return False
            logger.warning(
                f"[Lock] DynamoDB error acquiring '{lock_key}', proceeding without lock: {e}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"[Lock] Unexpected error acquiring '{lock_key}', proceeding without lock: {e}"
            )
            return True

    def release(self, lock_key: str):
        """락 해제 (자신이 소유한 경우만)."""
        try:
            run_with_irsa(TABLE_NAME, lambda t: t.delete_item(
                Key={"lock_key": lock_key},
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":owner": self._owner},
            ))
            logger.info(f"[Lock] Released '{lock_key}'")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.debug(f"[Lock] '{lock_key}' not owned by this pod, skip release")
            else:
                logger.error(f"[Lock] Failed to release '{lock_key}': {e}")
        except Exception as e:
            logger.error(f"[Lock] Failed to release '{lock_key}': {e}")
