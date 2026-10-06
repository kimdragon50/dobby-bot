"""Mention Handler - @dobby 멘션/DM → AgentCore Supervisor 호출"""
import json
import re
import threading

import boto3
from botocore.config import Config as BotocoreConfig
from slack_sdk.errors import SlackApiError

from src._shared.config import Config
from src._shared.logger import logger

# Markdown 수평선 (---, ***, ___)
_HR_RE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$")
# Markdown 표 구분행 (|---|:---:|)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")

# 답변 하단 피드백 안내 — 리액션만으로는 평가 방법을 알 수 없다
_FEEDBACK_HINT = (
    "\n\n────────────────────\n"
    "_이 답변이 도움이 되셨나요? 아래 :+1: / :-1: 를 눌러주세요_ "
    "_(:-1: 를 누르면 도비가 이유를 여쭤봅니다)_"
)


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _split_row(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


class MentionHandler:
    """@dobby 멘션과 DM을 AgentCore Supervisor Runtime에 라우팅하는 핸들러"""

    def __init__(self):
        self.agentcore = boto3.client(
            "bedrock-agentcore",
            region_name="ap-northeast-2",
            config=BotocoreConfig(
                connect_timeout=10,
                read_timeout=120,
                retries={"max_attempts": 1},
            ),
        )
        self.supervisor_arn = Config.AGENTCORE_SUPERVISOR_ARN

    def register_handlers(self, app, message_interceptors=None, on_answer=None):
        """app_mention 및 DM 이벤트 핸들러 등록

        message_interceptors: app_mention/DM을 supervisor로 보내기 전에 호출되는
        callable 목록 — fn(event, client) -> bool. True를 반환하면 해당
        메시지를 소비한 것으로 보고 디스패치하지 않는다 (예: 👎 사유 답글).

        on_answer: 답변 전송 직후 호출되는 callable — fn(client, channel, ts).
        피드백 씨앗 리액션(👍/👎)을 다는 데 쓴다.
        """
        interceptors = list(message_interceptors or [])
        self._on_answer = on_answer

        def intercepted(event, client) -> bool:
            for intercept in interceptors:
                try:
                    if intercept(event, client):
                        return True
                except Exception as e:
                    logger.warning(f"[Mention] 인터셉터 오류 (무시하고 계속): {e}")
            return False

        @app.event("app_mention")
        def handle_mention(event, client, say):
            if intercepted(event, client):
                return
            self._dispatch(event, client, say)

        @app.event("message")
        def handle_dm(event, client, say):
            # DM 채널(im)에서 온 메시지만 처리 (봇 자신의 메시지 제외)
            if event.get("channel_type") == "im" and not event.get("bot_id"):
                if intercepted(event, client):
                    return
                self._dispatch(event, client, say)

        logger.info("[Mention] Handlers registered (app_mention + DM)")

    def _dispatch(self, event, client, say):
        """백그라운드 스레드로 처리 (Slack 3초 타임아웃 회피)"""
        if not self.supervisor_arn:
            logger.warning("[Mention] AGENTCORE_SUPERVISOR_ARN not set, skipping")
            return

        threading.Thread(
            target=self._process,
            args=(event, client, say),
            daemon=True,
        ).start()

    def _process(self, event, client, say):
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")

        # @dobby 멘션 태그 제거
        raw_text = event.get("text") or ""
        text = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

        if not text:
            say(text="무엇을 도와드릴까요?", thread_ts=thread_ts)
            return

        # 스레드 단위 세션: 같은 스레드 = 하나의 대화
        session_id = f"slack-{channel}-{thread_ts}"

        logger.info(f"[Mention] user={user_id}, session={session_id}, text={text[:80]!r}")

        # 요청 메시지에 처리 중 리액션을 달아 스레드에 안내 메시지를 남기지 않는다.
        request_ts = event["ts"]
        eyes_added = False
        try:
            client.reactions_add(
                channel=channel,
                timestamp=request_ts,
                name="eyes",
            )
            eyes_added = True
        except Exception as e:
            logger.warning(f"[Mention] 처리 중 리액션 추가 실패: {e}")

        try:
            payload = self._build_payload(text)
            resp = self.agentcore.invoke_agent_runtime(
                agentRuntimeArn=self.supervisor_arn,
                runtimeSessionId=session_id,
                payload=payload,
            )
            raw = resp["response"].read()
            answer = self._md_to_slack(self._extract_text(raw))
            # 피드백 창구 안내 — 씨앗 리액션이 붙어도 용도를 모르면 안 누른다
            if self._on_answer_enabled():
                answer += _FEEDBACK_HINT

            sent = say(text=answer, thread_ts=thread_ts)
            answer_ts = sent.get("ts") if isinstance(sent, dict) else None
            logger.info(f"[Mention] Response sent ({len(answer)} chars)")

            # 피드백 창구를 눈에 보이게 — 사용자는 클릭 한 번으로 평가한다
            on_answer = getattr(self, "_on_answer", None)
            if on_answer and answer_ts:
                try:
                    on_answer(client, channel, answer_ts)
                except Exception as e:
                    logger.warning(f"[Mention] 피드백 리액션 부착 실패: {e}")

        except Exception as e:
            logger.exception(f"[Mention] AgentCore 호출 실패: {e}")
            err_text = "죄송해요, 잠시 문제가 생겼어요. 다시 시도해 주세요."
            say(text=err_text, thread_ts=thread_ts)
        finally:
            if eyes_added:
                try:
                    client.reactions_remove(
                        channel=channel,
                        timestamp=request_ts,
                        name="eyes",
                    )
                except Exception as e:
                    logger.warning(f"[Mention] 처리 중 리액션 제거 실패: {e}")

    def _on_answer_enabled(self) -> bool:
        """피드백 기능이 배선돼 있을 때만 안내를 붙인다."""
        return getattr(self, "_on_answer", None) is not None

    @staticmethod
    def _build_payload(text: str) -> bytes:
        """supervisor entry_point가 기대하는 HTTP 포맷 페이로드 생성"""
        return json.dumps({"prompt": text}, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _extract_text(raw: bytes) -> str:
        """A2A SSE 스트리밍 응답에서 텍스트 추출

        AgentCore는 SSE 형식으로 응답을 스트리밍합니다:
          data: {"type":"TEXT_MESSAGE_CONTENT","delta":"..."}
        TEXT_MESSAGE_CONTENT 이벤트의 delta를 모두 이어붙여 최종 텍스트를 반환합니다.
        """
        text = raw.decode("utf-8", errors="replace")

        # SSE 스트리밍 파싱: data: {...} 라인에서 TEXT_MESSAGE_CONTENT delta 수집
        deltas = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "TEXT_MESSAGE_CONTENT":
                delta = event.get("delta", "")
                if delta:
                    deltas.append(delta)

        if deltas:
            return "".join(deltas)

        # fallback: 단일 JSON 응답 (A2A artifacts 포맷)
        try:
            doc = json.loads(text)
            result = doc.get("result", doc)
            for art in result.get("artifacts", []) or []:
                for part in art.get("parts", []) or []:
                    if part.get("kind") == "text" and part.get("text"):
                        return part["text"]
        except json.JSONDecodeError:
            pass

        return text

    @staticmethod
    def _md_to_slack(text: str) -> str:
        """Markdown → Slack mrkdwn 변환

        Slack mrkdwn 은 일반 Markdown 과 다르다:
          **bold** → *bold*,  ### 제목 → *제목*,  [텍스트](url) → <url|텍스트>
          ``` 뒤의 언어 힌트(```bash)는 지원하지 않아 그대로 노출된다.

        코드 블록 안의 내용은 변환하지 않는다 — 코드에 들어 있는 `**` 나 `#` 를
        건드리면 코드가 깨진다.
        """
        lines = text.split("\n")
        out = []
        in_code = False
        table_headers = []

        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("```"):
                # Slack 은 ```lang 형식을 모른다 — 언어 힌트를 떼어낸다.
                # 여는 펜스에 붙은 내용이 있으면 다음 줄로 내린다 (```bash echo → ``` \n echo)
                rest = stripped[3:].strip()
                out.append("```")
                if not in_code and rest:
                    # 언어 이름만 있으면 버리고, 코드가 붙어 있으면 살린다
                    if " " in rest or not rest.isalnum():
                        out.append(rest)
                in_code = not in_code
                continue

            if in_code:
                out.append(line)
                continue

            # 표 구분행(|---|---|)은 Slack 에 의미가 없다 — 버린다
            if _TABLE_SEP_RE.match(line):
                continue

            # 표 데이터행 → 헤더를 라벨로 붙여 세로로 편다
            # (Slack 은 표를 렌더링하지 못해 그대로 두면 파이프가 그대로 보인다)
            if _is_table_row(line):
                # 다음 줄이 구분행이면 이 줄이 헤더다 — 라벨로 쓰고 출력하지 않는다
                if idx + 1 < len(lines) and _TABLE_SEP_RE.match(lines[idx + 1]):
                    table_headers = _split_row(line)
                    continue
                cells = _split_row(line)
                if table_headers and len(cells) > 1:
                    head, *rest = cells
                    body = [f"*{head}*"] + [
                        f"    • {table_headers[i + 1]}: {c}" if i + 1 < len(table_headers) else f"    • {c}"
                        for i, c in enumerate(rest)
                        if c
                    ]
                    out.append("\n".join(body))
                else:
                    out.append(" · ".join(c for c in cells if c))
                continue
            table_headers = []

            converted = line
            # 수평선(---, ***, ___) → Slack 에는 문법이 없으므로 선 문자로 대체
            if _HR_RE.match(line):
                out.append("────────────────────")
                continue
            # [텍스트](url) → <url|텍스트>
            converted = re.sub(
                r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", converted
            )
            # **bold** / __bold__ → *bold* (줄 단위 — Slack 포맷은 줄을 넘지 않는다)
            converted = re.sub(r"\*\*(.+?)\*\*", r"*\1*", converted)
            converted = re.sub(r"__(.+?)__", r"*\1*", converted)
            # ### 제목 → *제목*
            converted = re.sub(r"^(\s*)#{1,6}\s+(.+)$", r"\1*\2*", converted)
            # 남은 짝 없는 ** 는 Slack 에서 그대로 노출되므로 제거
            converted = converted.replace("**", "*")
            out.append(converted)

        result = "\n".join(out)
        # 코드 블록이 열린 채 끝났으면 닫아준다 (안 닫으면 이후 전체가 코드로 보인다)
        if in_code:
            result += "\n```"
        return result
