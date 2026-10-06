"""Feedback Handler — 봇 답변에 달린 👍/👎 리액션을 Langfuse score로 기록

@dobby 멘션/DM 답변(supervisor 경유)에 사용자가 👍/👎를 달면:
  1. 봇이 작성한 메시지인지 확인 (item_user == bot_user_id)
  2. 스레드 root ts를 역산해 supervisor session_id(slack-{channel}-{root_ts}) 구성
  3. Langfuse에 user-feedback score를 세션 단위로 upsert

score_id는 사용자×메시지 결정적 uuid5 — 같은 답변에 대한 번복(👍→👎)과
중복 리액션이 멱등 처리된다.
"""
import threading
import uuid

from src._shared.config import Config
from src._shared.langfuse_client import create_feedback_score
from src._shared.logger import logger
from src.feedback.store import PendingFeedbackStore

_POSITIVE = {"+1", "thumbsup"}
_NEGATIVE = {"-1", "thumbsdown"}
_MENTION_REASON_PREFIX = "피드백:"

# app.py에서 라우터에 등록할 리액션 이름 목록
FEEDBACK_REACTIONS = sorted(_POSITIVE | _NEGATIVE)


class FeedbackHandler:
    """봇 답변 메시지의 👍/👎 리액션 → Langfuse user-feedback score"""

    # 👎 사유 되물음 문구
    _ASK_REASON_TEXT = (
        "<@{user}> 어떤 점이 아쉬웠는지 이 스레드에서 "
        "<@{bot}> `피드백: 아쉬운 점` 형식으로 남겨주시면 "
        "도비 개선에 반영돼요 🙏 (답글 한 번만 수집됩니다)"
    )

    def __init__(self, bot_user_id: str):
        self.bot_user_id = bot_user_id
        self.store = PendingFeedbackStore(Config.FEEDBACK_PENDING_TABLE)

    def warm(self):
        """기동 시 사유 대기 힌트 워밍 (best-effort)."""
        self.store.warm()

    def seed_reactions(self, client, channel: str, ts: str) -> None:
        """답변에 👍/👎 를 미리 달아 피드백 창구를 눈에 보이게 만든다.

        리액션은 UI 에 아무 안내가 없어 사용자가 방법을 모른다. 봇이 먼저
        달아두면 클릭 한 번으로 끝난다. 이 씨앗 리액션은 event["user"] 가
        봇이므로 handle_reaction 에서 피드백으로 집계되지 않는다.
        """
        for name in ("+1", "-1"):
            try:
                client.reactions_add(channel=channel, timestamp=ts, name=name)
            except Exception as e:
                # already_reacted 등은 무해하므로 로그만 남긴다
                logger.debug(f"[Feedback] 씨앗 리액션({name}) 실패: {e}")

    def handle_reaction(self, event, client, say):
        # 봇이 작성한 답변에 달린 리액션만 피드백으로 취급
        if event.get("item_user") != self.bot_user_id:
            return
        # 봇이 스스로 단 씨앗 리액션은 피드백이 아니다
        if event.get("user") == self.bot_user_id:
            return
        threading.Thread(
            target=self._process,
            args=(event, client),
            name=f"feedback_{event.get('item', {}).get('ts', '')}",
            daemon=True,
        ).start()

    def _process(self, event, client):
        # 스킨톤 변형(+1::skin-tone-2)은 라우터에서 정규화되지만 방어적으로 재처리
        reaction = (event.get("reaction") or "").split("::")[0]
        if reaction in _POSITIVE:
            value = 1
        elif reaction in _NEGATIVE:
            value = -1
        else:
            return

        channel = event.get("item", {}).get("channel", "")
        answer_ts = event.get("item", {}).get("ts", "")
        user_id = event.get("user", "")
        if not channel or not answer_ts:
            return

        root_ts = self._resolve_root_ts(client, channel, answer_ts)
        # supervisor 세션 규칙과 동일 (src/mention/handler.py 참조)
        session_id = f"slack-{channel}-{root_ts}"
        # 사용자×메시지 결정적 id — 번복/중복 멱등 upsert
        score_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"slack|{channel}|{answer_ts}|{user_id}"))

        ok = create_feedback_score(
            score_id=score_id,
            value=value,
            session_id=session_id,
            comment=f"answer_ts={answer_ts}",
            metadata={
                "source": "slack",
                "channel": channel,
                "message_ts": answer_ts,
                "user_id": user_id,
            },
        )
        if ok:
            logger.info(
                f"[Feedback] score 기록: session={session_id}, value={value}, user={user_id}"
            )

        thread_key = f"{channel}#{root_ts}#{user_id}"
        if value == -1:
            # 이미 되물은 스레드면 중복 질문하지 않음 (score는 위에서 이미 upsert)
            if not self.store.has_hint(thread_key):
                try:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=root_ts,
                        text=self._ASK_REASON_TEXT.format(
                            user=user_id, bot=self.bot_user_id
                        ),
                    )
                except Exception as e:
                    logger.warning(f"[Feedback] 사유 되물음 전송 실패: {e}")
                self.store.put(thread_key, score_id, session_id, answer_ts)
        else:
            # 👎 → 👍 번복이면 사유 대기 취소
            if self.store.has_hint(thread_key):
                self.store.delete(thread_key)

    def handle_message(self, event, client, say):
        """채널 message 이벤트 — 사유 대기 중인 스레드 답글만 소비.

        DM(im)은 MentionHandler의 인터셉터 경로에서 처리하므로 여기서 제외
        (이중 소비 방지).
        """
        if event.get("channel_type") == "im":
            return
        self.try_consume_reply(event, client)

    def try_consume_reply(self, event, client) -> bool:
        """사유 대기 중인 사용자의 스레드 답글이면 사유로 소비하고 True.

        MentionHandler가 app_mention/DM 디스패치 전에 호출한다 — True면
        supervisor로 보내지 않는다. 채널 message 이벤트를 구독한 환경에서는
        멘션 없는 일반 답글도 handle_message 경유로 소비한다.
        """
        user_id = event.get("user") or ""
        if not user_id or event.get("bot_id"):
            return False
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return False
        channel = event.get("channel") or ""
        thread_key = f"{channel}#{thread_ts}#{user_id}"

        # 인메모리 힌트로 O(1) 배제 — 미스면 DDB 미조회
        if not self.store.has_hint(thread_key):
            return False

        text = (event.get("text") or "").strip()
        bot_mention = f"<@{self.bot_user_id}>"
        if bot_mention in text:
            # app_mention 은 확실히 전달되므로 피드백 전용 접두사로 사유를 받는다.
            # 접두사가 없으면 기존처럼 새 질문으로 보고 supervisor에 전달한다.
            text = text.replace(bot_mention, "").strip()
            if not text.startswith(_MENTION_REASON_PREFIX):
                return False
            text = text[len(_MENTION_REASON_PREFIX):].strip()
        if not text:
            return False

        pending = self.store.get(thread_key)
        if not pending:
            self.store.delete(thread_key)  # 힌트 정리
            return False

        ok = create_feedback_score(
            score_id=pending["score_id"],
            value=-1,
            session_id=pending["session_id"],
            comment=f"answer_ts={pending['answer_ts']}\nreason: {text[:2000]}",
            metadata={
                "source": "slack",
                "channel": channel,
                "message_ts": pending["answer_ts"],
                "user_id": user_id,
            },
        )
        if not ok:
            logger.warning(
                f"[Feedback] 👎 사유 기록 실패 — 대기 유지: "
                f"session={pending['session_id']}, user={user_id}"
            )
            return True

        self.store.delete(thread_key)
        logger.info(
            f"[Feedback] 👎 사유 수집: session={pending['session_id']}, user={user_id}"
        )
        # 접수 표시 — 사용자가 수집 여부를 알 수 있게
        try:
            client.reactions_add(
                channel=channel, timestamp=event.get("ts", ""), name="white_check_mark"
            )
        except Exception as e:
            logger.warning(f"[Feedback] 접수 리액션 실패: {e}")
        return True

    @staticmethod
    def _resolve_root_ts(client, channel: str, message_ts: str) -> str:
        """리액션이 달린 메시지에서 스레드 root ts를 역산한다.

        스레드 답글이면 thread_ts가 root, 아니면 자기 자신이 root.
        (complete_ticket/handler.py의 역산 패턴과 동일)
        """
        try:
            resp = client.conversations_replies(
                channel=channel, ts=message_ts, limit=1, inclusive=True
            )
            msg = resp["messages"][0]
            return msg.get("thread_ts") or msg["ts"]
        except Exception as e:
            logger.warning(f"[Feedback] 스레드 root 조회 실패, 메시지 ts 사용: {e}")
            return message_ts
