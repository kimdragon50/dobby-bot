"""PendingFeedbackStore — 👎 사유 대기 상태 저장소

👎 리액션 후 봇이 스레드에서 사유를 되물으면, 사용자의 다음 답글을 사유로
수집하기까지의 대기 상태를 DynamoDB에 저장한다. 봇 재시작(배포)에도 대기가
유지돼야 "물어봤는데 답해도 무반응"을 방지한다.

테이블: dobby-feedback-pending
  PK thread_key(S) = "{channel}#{root_ts}#{user_id}"
  attrs: score_id, session_id, answer_ts, ttl(N — DynamoDB TTL 자동 삭제)

인메모리 힌트 set: message 이벤트는 봇이 있는 모든 채널의 모든 메시지에
발화하므로, 메시지마다 DDB를 조회하지 않도록 쓰기 시 set에 추가하고 기동 시
scan 1회로 워밍한다. set 미스면 DDB 미조회로 즉시 배제.

모든 DDB 실패는 경고 로그 + 우아한 성능 저하 — 피드백 기능이 봇을 죽이면
안 된다 (DDB 불가 시 인메모리 힌트만으로 재시작 전까지는 동작).
"""
import threading
import time
from typing import Dict, Optional

from src._shared.dynamo import run_with_irsa
from src._shared.logger import logger

# 사유 대기 TTL — DM에서 멘션 없는 새 질문이 사유로 오수집되는 리스크를
# 줄이기 위해 짧게 유지한다 (대부분의 사유 답글은 수 분 내 작성됨)
PENDING_TTL_SECONDS = 2 * 60 * 60  # 2h


class PendingFeedbackStore:
    """👎 사유 대기 상태 저장소 (DynamoDB + 인메모리 힌트)"""

    def __init__(self, table_name: str):
        self.table_name = table_name
        self._hints: set = set()
        self._lock = threading.Lock()

    def warm(self) -> None:
        """기동 시 DDB의 활성 대기 항목으로 힌트 set을 워밍한다 (best-effort)."""
        try:
            resp = run_with_irsa(self.table_name, lambda t: t.scan(
                ProjectionExpression="thread_key"
            ))
            keys = {item["thread_key"] for item in resp.get("Items", [])}
            with self._lock:
                self._hints |= keys
            logger.info(f"[Feedback] 대기 힌트 워밍: {len(keys)}건")
        except Exception as e:
            logger.warning(f"[Feedback] 대기 힌트 워밍 실패 (신규 대기만 수집됨): {e}")

    def put(self, thread_key: str, score_id: str, session_id: str, answer_ts: str) -> None:
        """사유 대기 등록. DDB 실패해도 인메모리 힌트는 유지 (재시작 전까지 동작)."""
        with self._lock:
            self._hints.add(thread_key)
        try:
            run_with_irsa(self.table_name, lambda t: t.put_item(Item={
                "thread_key": thread_key,
                "score_id": score_id,
                "session_id": session_id,
                "answer_ts": answer_ts,
                "ttl": int(time.time()) + PENDING_TTL_SECONDS,
            }))
        except Exception as e:
            logger.warning(f"[Feedback] 대기 상태 DDB 저장 실패 (인메모리로만 유지): {e}")

    def get(self, thread_key: str) -> Optional[Dict]:
        """대기 항목 조회. TTL 만료 항목은 없는 것으로 취급."""
        try:
            resp = run_with_irsa(self.table_name, lambda t: t.get_item(
                Key={"thread_key": thread_key}
            ))
            item = resp.get("Item")
            # DynamoDB TTL 삭제는 지연될 수 있으므로 만료 검사를 직접 수행
            if item and int(item.get("ttl", 0)) < int(time.time()):
                return None
            return item
        except Exception as e:
            logger.warning(f"[Feedback] 대기 상태 조회 실패: {e}")
            return None

    def delete(self, thread_key: str) -> None:
        with self._lock:
            self._hints.discard(thread_key)
        try:
            run_with_irsa(self.table_name, lambda t: t.delete_item(
                Key={"thread_key": thread_key}
            ))
        except Exception as e:
            logger.warning(f"[Feedback] 대기 상태 삭제 실패 (TTL로 자동 정리됨): {e}")

    def has_hint(self, thread_key: str) -> bool:
        """인메모리 힌트 확인 — DDB 미조회 빠른 배제용."""
        with self._lock:
            return thread_key in self._hints
