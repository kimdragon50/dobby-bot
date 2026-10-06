"""Daily Scrum Handler — 스크럼 알림 게시 / 작성 모달 / 전체 종합

흐름:
  1) 스케줄러(또는 /dobby scrum now)가 채널에 @channel 알림을 게시
  2) 사용자가 "스크럼 작성하기" 버튼 → 모달 → 제출 시 스레드에 답글
  3) "전체 종합 정리" 버튼 → 스레드 답글을 모아 LLM 이 종합

모달을 쓰는 이유: 자유 양식으로 받으면 항목이 빠지거나 형식이 제각각이라
종합 품질이 떨어진다. 직접 쓰고 싶은 사람을 위해 복붙 템플릿도 함께 제공한다.
"""
import asyncio
import json
import re
import threading
from datetime import datetime, timezone, timedelta

from src._shared.config import Config
from src._shared.logger import logger
from src.command.daily_scrum import blocks as B
from src.command.daily_scrum import sprint as S
from src.command.daily_scrum import tickets as T
from src.command.daily_scrum.channel_store import ScrumChannelStore
from src.command.daily_scrum.prompts import (
    SUMMARY_SYSTEM_PROMPT,
    SUMMARY_HUMAN_PROMPT,
)

KST = timezone(timedelta(hours=9))

# 종합 대상에서 제외할 답글: 도비가 올린 것(종합 결과·안내)
_SKIP_PREFIXES = ("📋", "*데일리 스크럼 종합", "🎫", "*오늘의 티켓 현황")


class DailyScrumHandler:
    """데일리 스크럼 알림·수집·종합"""

    def __init__(self, app, llm_service, jira_service=None):
        self.app = app
        self.llm = llm_service
        self.jira = jira_service
        self.channels = ScrumChannelStore()

    # ── 등록 ────────────────────────────────────────────────

    def register_handlers(self):
        self.app.action(B.ACTION_OPEN_MODAL)(self._handle_open_modal)
        self.app.action(B.ACTION_SUMMARIZE)(self._handle_summarize)
        self.app.view(B.CALLBACK_SUBMIT)(self._handle_submit)
        logger.info("[Scrum] Handlers registered (modal + buttons)")

    # ── 알림 게시 ───────────────────────────────────────────

    def post_announcement(self, client, channel: str, mention_channel: bool = True) -> str:
        """스크럼 알림을 채널에 게시하고 ts 를 반환한다."""
        date_label = B.format_date(datetime.now(KST))
        resp = client.chat_postMessage(
            channel=channel,
            text=f"{'<!channel> ' if mention_channel else ''}데일리 스크럼 — {date_label}",
            blocks=B.announcement_blocks(date_label, mention_channel),
        )
        ts = resp.get("ts", "")
        logger.info(f"[Scrum] 알림 게시: channel={channel}, ts={ts}")
        # 티켓 현황은 스레드 첫 답글로 — 각자 스크럼 쓸 때 참고하도록.
        # Jira 조회가 느리거나 실패해도 알림 자체는 이미 올라가 있다.
        if ts:
            self.post_ticket_status(client, channel, ts)
        return ts

    def _fetch_sprint_tickets(self, jql: str):
        """JQL 조회 후 가장 최근 스프린트로 좁힌다.

        Returns:
            (issues, total, sprint_label) — total 은 좁힌 뒤 기준.
            스프린트를 안 쓰는 프로젝트면 전량 + 빈 라벨.
        """
        issues, total = self.jira.search_issues_by_jql(jql)
        if not Config.SCRUM_SPRINT_ONLY:
            return issues, total, ""

        sprint = S.latest_sprint(issues)
        if not sprint:
            logger.info("[Scrum] 스프린트 정보 없음 — 전체 티켓으로 표시")
            return issues, total, ""

        scoped = S.filter_by_sprint(issues, sprint)
        logger.info(
            f"[Scrum] 스프린트 '{sprint.get('name')}'({sprint['state']}) "
            f"기준 {len(scoped)}건 / 전체 {total}건"
        )
        return scoped, len(scoped), S.sprint_label(sprint)

    def post_ticket_status(self, client, channel: str, thread_ts: str) -> bool:
        """JQL 로 조회한 티켓을 담당자별로 정리해 스레드에 올린다."""
        jql = (Config.SCRUM_JIRA_JQL or "").strip()
        if not jql or not self.jira:
            logger.info("[Scrum] 티켓 현황 비활성 (JQL 미설정 또는 Jira 미초기화)")
            return False
        try:
            issues, total, sprint_label = self._fetch_sprint_tickets(jql)
        except Exception as e:
            logger.error(f"[Scrum] 티켓 조회 실패: {e}")
            return False

        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"오늘의 티켓 현황 ({total or len(issues)}건)",
                blocks=T.ticket_blocks(issues, total=total, sprint_label=sprint_label),
            )
            logger.info(
                f"[Scrum] 티켓 현황 게시: {len(issues)}/{total}건, thread={thread_ts}"
            )
            return True
        except Exception as e:
            logger.error(f"[Scrum] 티켓 현황 게시 실패: {e}")
            return False

    # ── /dobby scrum ────────────────────────────────────────

    def handle_subcommand(self, command: dict, client):
        """`/dobby scrum <서브커맨드>`

        on / off / list : 자동 게시 채널 등록·해제·확인
        now / test      : 수동 게시 (test 는 멘션 없이)
        """
        text = (command.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        channel = command["channel_id"]
        user_id = command["user_id"]

        if sub == "test":
            self.post_announcement(client, channel, mention_channel=False)
            return
        if sub == "now":
            self.post_announcement(client, channel, mention_channel=True)
            return
        if sub in ("on", "등록"):
            self._register_channel(client, command)
            return
        if sub in ("off", "해제"):
            self._unregister_channel(client, channel, user_id)
            return
        if sub in ("list", "확인", "목록"):
            self._list_channels(client, channel, user_id)
            return
        if sub in ("tickets", "티켓"):
            # 이 채널의 가장 최근 스크럼 스레드에 티켓 현황을 다시 올린다
            self._post_tickets_manually(client, channel, user_id)
            return

        self._ephemeral(client, channel, user_id, self._help_text())

    @staticmethod
    def _help_text() -> str:
        cmd = Config.SLACK_COMMAND
        hh, mm = Config.SCRUM_HOUR, Config.SCRUM_MINUTE
        return (
            ":spiral_note_pad: *데일리 스크럼*\n\n"
            "*자동 게시 설정*\n"
            f"• `{cmd} scrum on` — 이 채널을 자동 게시 대상으로 등록\n"
            f"• `{cmd} scrum off` — 이 채널 등록 해제\n"
            f"• `{cmd} scrum list` — 등록된 채널 목록 확인\n\n"
            "*수동 게시*\n"
            f"• `{cmd} scrum now` — 지금 바로 게시 (@channel 멘션 포함)\n"
            f"• `{cmd} scrum test` — 멘션 없이 미리보기\n"
            f"• `{cmd} scrum tickets` — 담당자별 티켓 현황만 게시\n\n"
            f"_자동 게시는 평일(월~금) 오전 {hh}시 {mm:02d}분에 실행됩니다._"
        )

    def _post_tickets_manually(self, client, channel: str, user_id: str):
        """`/dobby scrum tickets` — 티켓 현황만 다시 게시한다.

        스레드가 아니라 채널에 바로 올린다. 스크럼 시간이 아닐 때도
        "지금 누가 뭘 들고 있나" 를 확인하는 용도다.
        """
        if not (Config.SCRUM_JIRA_JQL or "").strip() or not self.jira:
            self._ephemeral(
                client, channel, user_id,
                ":information_source: 티켓 현황이 설정돼 있지 않습니다 (SCRUM_JIRA_JQL 미설정).",
            )
            return
        try:
            issues, total, sprint_label = self._fetch_sprint_tickets(
                Config.SCRUM_JIRA_JQL.strip()
            )
        except Exception as e:
            logger.error(f"[Scrum] 수동 티켓 조회 실패: {e}")
            self._ephemeral(client, channel, user_id, ":x: 티켓 조회에 실패했어요. 잠시 후 다시 시도해주세요.")
            return
        try:
            client.chat_postMessage(
                channel=channel,
                text=f"티켓 현황 ({total or len(issues)}건)",
                blocks=T.ticket_blocks(issues, total=total, sprint_label=sprint_label),
            )
        except Exception as e:
            logger.error(f"[Scrum] 수동 티켓 게시 실패: {e}")

    def _register_channel(self, client, command: dict):
        channel = command["channel_id"]
        user_id = command["user_id"]
        channel_name = command.get("channel_name", "") or channel

        if self.channels.is_registered(channel):
            self._ephemeral(
                client, channel, user_id,
                f":information_source: 이 채널은 이미 등록돼 있습니다. "
                f"해제하려면 `{Config.SLACK_COMMAND} scrum off`",
            )
            return

        if not self.channels.register(channel, channel_name, user_id):
            self._ephemeral(client, channel, user_id, ":x: 등록에 실패했어요. 잠시 후 다시 시도해주세요.")
            return

        hh, mm = Config.SCRUM_HOUR, Config.SCRUM_MINUTE
        # 등록은 채널 전체가 알아야 하므로 공개 메시지로 남긴다
        client.chat_postMessage(
            channel=channel,
            text=(
                f":white_check_mark: 이 채널에 데일리 스크럼 자동 게시가 등록되었습니다.\n"
                f"평일(월~금) 오전 {hh}시 {mm:02d}분에 도비가 스크럼 알림을 올립니다. "
                f"(등록: <@{user_id}>)"
            ),
        )

    def _unregister_channel(self, client, channel: str, user_id: str):
        if not self.channels.is_registered(channel):
            self._ephemeral(client, channel, user_id, ":information_source: 이 채널은 등록돼 있지 않습니다.")
            return
        if not self.channels.unregister(channel):
            self._ephemeral(client, channel, user_id, ":x: 해제에 실패했어요. 잠시 후 다시 시도해주세요.")
            return
        client.chat_postMessage(
            channel=channel,
            text=f":wave: 데일리 스크럼 자동 게시를 해제했습니다. (해제: <@{user_id}>)",
        )

    def _list_channels(self, client, channel: str, user_id: str):
        items = self.channels.list_all()
        if not items:
            self._ephemeral(
                client, channel, user_id,
                f":information_source: 등록된 채널이 없습니다. "
                f"등록하려면 해당 채널에서 `{Config.SLACK_COMMAND} scrum on`",
            )
            return
        hh, mm = Config.SCRUM_HOUR, Config.SCRUM_MINUTE
        lines = [f":spiral_note_pad: *자동 게시 채널* ({len(items)}개) — 평일 오전 {hh}시 {mm:02d}분"]
        for it in sorted(items, key=lambda x: x.get("channel_name", "")):
            cid = it.get("channel_id", "")
            by = it.get("registered_by", "")
            at = (it.get("registered_at", "") or "")[:10]
            lines.append(f"• <#{cid}>" + (f" — <@{by}> 등록 ({at})" if by else ""))
        self._ephemeral(client, channel, user_id, "\n".join(lines))

    # ── 버튼: 모달 열기 ─────────────────────────────────────

    def _handle_open_modal(self, ack, body, client):
        ack()
        channel = body["channel"]["id"]
        user_id = body["user"]["id"]
        # 버튼이 달린 메시지가 스크럼 알림 = 스레드 루트
        msg = body.get("message", {}) or {}
        thread_ts = msg.get("thread_ts") or msg.get("ts", "")

        # 이미 쓴 글이 있으면 그 내용을 불러와 수정 모드로 연다.
        # 봇이 올린 글이라 사용자가 Slack UI 로 직접 고칠 수 없기 때문이다.
        entry_ts, prev = self._find_user_entry(client, channel, thread_ts, user_id)
        meta = json.dumps(
            {"channel": channel, "thread_ts": thread_ts, "entry_ts": entry_ts or ""}
        )
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=B.submit_modal(meta, values=prev, editing=bool(entry_ts)),
            )
        except Exception as e:
            logger.error(f"[Scrum] 모달 열기 실패: {e}")

    def _find_user_entry(self, client, channel: str, thread_ts: str, user_id: str):
        """스레드에서 이 사용자가 쓴 스크럼 글의 (ts, 입력값) 을 찾는다.

        메시지 metadata 에 원본 입력을 넣어두므로 텍스트를 역파싱하지 않는다.
        같은 사람이 여러 번 썼으면 가장 마지막 글을 대상으로 한다.
        """
        if not thread_ts:
            return None, {}
        try:
            resp = client.conversations_replies(
                channel=channel, ts=thread_ts, limit=200, include_all_metadata=True
            )
        except Exception as e:
            logger.warning(f"[Scrum] 기존 스크럼 조회 실패: {e}")
            return None, {}

        found_ts, found_values = None, {}
        for m in resp.get("messages", []):
            md = m.get("metadata") or {}
            if md.get("event_type") != B.ENTRY_EVENT_TYPE:
                continue
            payload = md.get("event_payload") or {}
            if payload.get("user") != user_id:
                continue
            found_ts = m.get("ts")
            found_values = {k: (payload.get(k) or "") for k, _, _, _ in B.FIELDS}
        return found_ts, found_values

    # ── 모달 제출 → 스레드 답글 ─────────────────────────────

    def _handle_submit(self, ack, body, client, view):
        ack()
        user_id = body["user"]["id"]
        try:
            meta = json.loads(view.get("private_metadata") or "{}")
        except json.JSONDecodeError:
            meta = {}
        channel = meta.get("channel", "")
        thread_ts = meta.get("thread_ts", "")
        if not channel or not thread_ts:
            logger.warning("[Scrum] 제출 메타데이터 없음 — 답글 생략")
            return

        state = view["state"]["values"]
        values = {}
        for key, _, _, _ in B.FIELDS:
            block = state.get(f"{key}_block", {})
            element = block.get(f"{key}_input", {})
            values[key] = (element.get("value") or "").strip()

        if not any(values.values()):
            return

        entry_ts = meta.get("entry_ts", "")
        common = dict(
            text=f"{user_id}님의 스크럼",
            blocks=B.entry_blocks(user_id, values),
            metadata=B.entry_metadata(user_id, values),
        )
        try:
            if entry_ts:
                # 기존 글을 갱신한다 — 새로 달면 같은 사람 글이 스레드에 쌓인다
                client.chat_update(channel=channel, ts=entry_ts, **common)
                logger.info(f"[Scrum] 스크럼 수정: user={user_id}, ts={entry_ts}")
            else:
                client.chat_postMessage(channel=channel, thread_ts=thread_ts, **common)
                logger.info(f"[Scrum] 스크럼 등록: user={user_id}, thread={thread_ts}")
        except Exception as e:
            logger.error(f"[Scrum] 답글 게시/수정 실패: {e}")

    # ── 버튼: 전체 종합 ─────────────────────────────────────

    def _handle_summarize(self, ack, body, client):
        ack()
        channel = body["channel"]["id"]
        msg = body.get("message", {}) or {}
        thread_ts = msg.get("thread_ts") or msg.get("ts", "")
        user_id = body["user"]["id"]
        # LLM 호출이 길어질 수 있어 백그라운드로 — Slack 3초 제한 회피
        threading.Thread(
            target=self._summarize,
            args=(client, channel, thread_ts, user_id),
            name=f"scrum_summary_{thread_ts}",
            daemon=True,
        ).start()

    def _summarize(self, client, channel: str, thread_ts: str, requester: str):
        try:
            entries = self._collect_entries(client, channel, thread_ts)
        except Exception as e:
            logger.error(f"[Scrum] 스레드 조회 실패: {e}")
            self._ephemeral(client, channel, requester, "스레드를 읽지 못했어요. 잠시 후 다시 시도해주세요.")
            return

        if not entries:
            self._ephemeral(
                client, channel, requester,
                "아직 등록된 스크럼이 없어요. 팀원들이 작성한 뒤에 종합해주세요.",
            )
            return

        body = "\n\n".join(f"<@{uid}>\n{text}" for uid, text in entries)
        date_label = B.format_date(datetime.now(KST))
        try:
            summary = asyncio.run(
                self.llm.invoke(
                    SUMMARY_SYSTEM_PROMPT,
                    SUMMARY_HUMAN_PROMPT,
                    {"date": date_label, "entries": body},
                )
            )
        except Exception as e:
            logger.error(f"[Scrum] 종합 생성 실패: {e}")
            self._ephemeral(client, channel, requester, "종합 정리 중 문제가 생겼어요. 다시 시도해주세요.")
            return

        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"데일리 스크럼 종합 — {date_label}",
                blocks=B.summary_blocks(date_label, len(entries), summary.strip()),
            )
            logger.info(f"[Scrum] 종합 게시: {len(entries)}명, thread={thread_ts}")
        except Exception as e:
            logger.error(f"[Scrum] 종합 게시 실패: {e}")

    # ── 내부 유틸 ───────────────────────────────────────────

    def _collect_entries(self, client, channel: str, thread_ts: str):
        """스레드 답글에서 사람별 최신 스크럼을 모은다.

        같은 사람이 여러 번 올렸으면 마지막 것만 쓴다 (수정 의도로 본다).
        봇이 올린 안내·종합 결과는 제외한다.
        """
        resp = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=200, include_all_metadata=True
        )
        latest = {}
        for m in resp.get("messages", []):
            if m.get("ts") == thread_ts:
                continue  # 루트(알림) 제외

            # 모달로 쓴 글은 metadata 에 원본 입력이 있다 — 텍스트 역파싱보다 정확하다
            md = m.get("metadata") or {}
            if md.get("event_type") == B.ENTRY_EVENT_TYPE:
                payload = md.get("event_payload") or {}
                uid = payload.get("user") or ""
                if uid:
                    parts = [
                        f"{label}: {payload.get(key)}"
                        for key, label, _, _ in B.FIELDS
                        if (payload.get(key) or "").strip()
                    ]
                    if parts:
                        latest[uid] = "\n".join(parts)
                    continue

            text = self._plain_text(m)
            if not text:
                continue
            if any(text.lstrip().startswith(p) for p in _SKIP_PREFIXES):
                continue
            uid = m.get("user") or ""
            if m.get("bot_id"):
                # 봇이 대신 올린 개인 스크럼은 본문에서 작성자를 찾는다
                found = re.search(r"<@([A-Z0-9]+)>님의 스크럼", text)
                if not found:
                    continue
                uid = found.group(1)
                text = text.split("\n", 1)[1] if "\n" in text else text
            if not uid:
                continue
            latest[uid] = text.strip()
        return list(latest.items())

    @staticmethod
    def _plain_text(message: dict) -> str:
        """message 의 본문 텍스트 — blocks 로 보낸 경우도 복원한다."""
        parts = []
        for blk in message.get("blocks", []) or []:
            if blk.get("type") == "section":
                t = (blk.get("text") or {}).get("text", "")
                if t:
                    parts.append(t)
        if parts:
            return "\n".join(parts)
        return message.get("text", "") or ""

    @staticmethod
    def _ephemeral(client, channel: str, user: str, text: str):
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=text)
        except Exception as e:
            logger.warning(f"[Scrum] ephemeral 전송 실패: {e}")
