"""Langfuse score 기록 클라이언트 — 사용자 피드백 수집용

LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 3종이 모두 설정된
경우에만 활성화된다. 미설정이거나 초기화에 실패하면 no-op — 피드백 기록
실패가 봇 동작을 막으면 안 된다.

supervisor가 Langfuse 트레이스에 langfuse_session_id = runtimeSessionId
(= slack-{channel}-{thread_ts})를 기록하므로, 같은 session_id로 score를
남기면 해당 스레드의 전체 대화 트레이스와 함께 조회된다.
"""
import threading
from typing import Optional

from src._shared.config import Config
from src._shared.logger import logger

_client = None
_initialized = False
_lock = threading.Lock()


def get_client():
    """Langfuse 클라이언트 싱글톤. 비활성/실패 시 None."""
    global _client, _initialized
    if _initialized:
        return _client
    with _lock:
        if _initialized:
            return _client
        _initialized = True

        host = Config.LANGFUSE_HOST.strip()
        public_key = Config.LANGFUSE_PUBLIC_KEY.strip()
        secret_key = Config.LANGFUSE_SECRET_KEY.strip()
        if not (host and public_key and secret_key):
            logger.info("[Langfuse] 비활성 — 환경변수 미설정 (피드백 기록 건너뜀)")
            return None

        try:
            from langfuse import Langfuse
            _client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)
            logger.info(f"[Langfuse] 활성: {host}")
        except Exception as e:
            logger.warning(f"[Langfuse] 초기화 실패 — 피드백 기록 비활성: {e}")
            _client = None
    return _client


def create_feedback_score(
    score_id: str,
    value: int,
    session_id: str,
    comment: str = "",
    metadata: Optional[dict] = None,
) -> bool:
    """user-feedback score를 세션 단위로 기록(upsert)한다.

    같은 score_id로 재호출하면 값이 갱신된다 — 사용자×메시지당 결정적 id를
    쓰면 👍→👎 번복과 중복 리액션이 자연스럽게 멱등 처리된다.
    """
    client = get_client()
    if client is None:
        return False
    try:
        client.create_score(
            score_id=score_id,
            name="user-feedback",
            value=value,
            session_id=session_id,
            data_type="NUMERIC",
            comment=comment or None,
            metadata=metadata or {},
        )
        client.flush()
        return True
    except Exception as e:
        logger.error(f"[Langfuse] score 기록 실패 (session={session_id}): {e}")
        return False
