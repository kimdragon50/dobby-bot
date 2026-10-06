"""Unified Router - Routes Slack commands and emoji reactions to handlers

Provides centralized routing for:
- Slash commands: /dobby jira, /dobby wkreport, etc.
- Emoji reactions: :op_cloud:, :op_analyze:, etc.
"""
import time
from typing import Callable, Dict, Optional, Set

from slack_bolt import App

from src._shared.config import Config
from src._shared.logger import logger


def get_help_text(command: str) -> str:
    """동적으로 HELP_TEXT 생성"""
    # Jira emoji mapping 가져오기 (config.yaml SoT)
    try:
        from src.emoji.jira_ticket.handler import ReactionConfig
        jira_mapping = ReactionConfig.get_reaction_mapping()
    except Exception:
        jira_mapping = {}

    # Jira emoji 섹션 동적 생성
    if jira_mapping:
        emoji_list = " ".join([f"`:{name}:`" for name in jira_mapping.keys()])
        mapping_lines = "\n".join([
            f"- {name} → {info['project_key']} 프로젝트"
            for name, info in jira_mapping.items()
        ])
        jira_emoji_section = f"""• {emoji_list} - Jira 티켓 자동 생성
```
- 스레드 내용을 AI가 분석하여 Jira 티켓 생성
{mapping_lines}
```"""
    else:
        jira_emoji_section = ""

    # 문의 채널 안내 (SUPPORT_CHANNEL_ID 설정 시에만 표시)
    support_section = ""
    if Config.SUPPORT_CHANNEL_ID:
        support_section = (
            "\n\n:speech_balloon: *문의/제안*\n"
            f"Dobby Bot 관련 문의는 <#{Config.SUPPORT_CHANNEL_ID}> 채널을 이용해주세요"
        )

    return f""":crystal_ball: *Dobby Bot 사용법*

:pushpin: *슬래시 명령어*
• `{command} ask <질문>` - Knowledge Base 질문하기 (채널에 공개)
• `{command} askpv <질문>` - Knowledge Base 질문하기 (나에게만 표시)
```
- 사내 문서(Knowledge Base) 기반으로 AI가 답변 생성
- 예: {command} ask 온보딩 가이드 문서 어디있어?
- askpv는 본인에게만 보이는 비공개 응답
```
• `{command} jira` - Jira 티켓 생성 모달 열기
```
- Slack 스레드 내용을 AI가 분석하여 Jira 티켓 초안 생성
- 프로젝트, 에픽, 담당자 등 선택 후 티켓 생성
- 체크리스트 항목 자동 추출
```
• `{command} wkreport` - 주간 리포트 생성 모달 열기
```
- Jira 그룹 선택하여 주간 업무 리포트 생성
- 기간 내 업데이트된 티켓을 에픽별로 그룹화
- AI가 요약하여 경영진 보고용/팀 내부 공유용 형식 제공
```
• `{command} food` (또는 `{command} bob`) - 오늘 뭐 먹지? 메뉴 추천
```
- 위치, 분위기, 예산 등을 입력하면 AI가 메뉴 3가지 추천
- 오늘 날씨를 자동 반영하여 맞춤 추천
- 네이버 블로그/지도 검색 링크 포함
```
• `{command} scrum` - 데일리 스크럼 알림
```
- scrum on / off / list: 자동 게시 채널 등록 / 해제 / 확인
- scrum now: 지금 바로 게시 (@channel) | scrum test: 멘션 없이 미리보기
- scrum tickets: 담당자별 Jira 티켓 현황 게시 (스크럼 알림에도 자동 포함)
  현재 스프린트 티켓만 표시 (진행 중인 스프린트가 없으면 직전 스프린트)
- 자동 게시는 평일 오전 9시
- 버튼으로 각자 작성하면 스레드에 자동 정리, 전체 종합도 버튼 하나로
```
• `{command} briefing` - 일정 브리핑 등록/관리
```
- 매일 오후 8시에 내일 Google Calendar 일정을 DM으로 브리핑
- briefing list: 등록 목록 | briefing test: 테스트 | briefing off: 해제
```

:speech_balloon: *AI 도비에게 질문 (@dobby 멘션/DM)*
• 채널에서 `@dobby 질문내용` 또는 도비에게 DM으로 질문
```
- 인프라/비용/보안 관련 질문에 AI 도비가 답변
- 예: @dobby 지난달 AWS 사용료 얼마 나왔어?
- 같은 스레드에서 이어서 질문하면 문맥 유지
- 답변이 도움됐다면 👍, 아쉬웠다면 👎 리액션을 눌러주세요
  (👎를 누르면 도비가 이유를 물어봐요 — 답글 한 줄이 도비를 성장시킵니다)
```

:pushpin: *이모지 리액션*
{jira_emoji_section}{support_section}"""


class SlackRouter:
    """Unified router for Slack commands and emoji reactions"""

    _DEDUP_TTL = 60  # seconds to remember seen events

    def __init__(self, app: App):
        self.app = app
        self.command = Config.SLACK_COMMAND  # /dobby or /dobby-test
        self._command_handlers: Dict[str, Callable] = {}
        self._reaction_handlers: Dict[str, Callable] = {}
        self._reaction_channels: Dict[str, Set[str]] = {}  # reaction -> allowed channels
        self._seen_events: Dict[str, float] = {}  # dedup key -> timestamp

    def register(self):
        """Register Slack event handlers"""
        # Register command from environment variable
        self.app.command(self.command)(self._handle_command)
        logger.info(f"SlackRouter: registered {self.command} command")

        # Register reaction_added event (single handler for all reactions)
        self.app.event("reaction_added")(self._handle_reaction)
        logger.info("SlackRouter: registered reaction_added event")

    # ===== Command Registration =====

    def command(self, name: str):
        """Decorator to register a subcommand handler

        Usage:
            @router.command('jira')
            def handle_jira(command, client):
                ...
        """
        def decorator(func: Callable):
            self._command_handlers[name] = func
            logger.info(f"SlackRouter: registered command '{name}'")
            return func
        return decorator

    def register_command(self, name: str, handler: Callable):
        """Register a subcommand handler directly

        Args:
            name: Subcommand name (e.g., 'jira', 'wkreport')
            handler: Callable that receives (command, client) arguments
        """
        self._command_handlers[name] = handler
        logger.info(f"SlackRouter: registered command '{name}'")

    # ===== Reaction Registration =====

    def reaction(self, name: str, allowed_channels: Optional[Set[str]] = None):
        """Decorator to register a reaction handler

        Usage:
            @router.reaction('op_analyze')
            def handle_op_analyze(event, client, say):
                ...

            @router.reaction('op_cloud', allowed_channels={'C123', 'C456'})
            def handle_op_cloud(event, client, say):
                ...
        """
        def decorator(func: Callable):
            self._reaction_handlers[name] = func
            if allowed_channels:
                self._reaction_channels[name] = allowed_channels
            logger.info(f"SlackRouter: registered reaction '{name}'")
            return func
        return decorator

    def register_reaction(self, name: str, handler: Callable, allowed_channels: Optional[Set[str]] = None):
        """Register a reaction handler directly

        Args:
            name: Reaction emoji name (e.g., 'op_cloud', 'op_analyze')
            handler: Callable that receives (event, client, say) arguments
            allowed_channels: Optional set of allowed channel IDs
        """
        self._reaction_handlers[name] = handler
        if allowed_channels:
            self._reaction_channels[name] = allowed_channels
        logger.info(f"SlackRouter: registered reaction '{name}'")

    # ===== Internal Handlers =====

    def _handle_command(self, ack, command, client):
        """Handle /dobby slash command - routes to appropriate subcommand"""
        logger.info(f"[SlackRouter] /dobby command received: {command.get('text', '')}")
        ack()

        text = command.get('text', '').strip()

        # Find matching subcommand
        for name, handler in self._command_handlers.items():
            if text == name or text.startswith(f'{name} '):
                try:
                    handler(command, client)
                except Exception as e:
                    logger.error(f"[SlackRouter] Error in command '{name}': {e}")
                    client.chat_postMessage(
                        channel=command['channel_id'],
                        text=f":x: '{name}' 명령어 처리 중 오류가 발생했습니다."
                    )
                return

        # No matching subcommand - show help
        self._show_help(client, command['channel_id'], command['user_id'])

    def _is_duplicate(self, key: str) -> bool:
        """Return True if this event key was already seen within TTL, else record it."""
        now = time.time()
        # Purge expired entries
        expired = [k for k, t in self._seen_events.items() if now - t > self._DEDUP_TTL]
        for k in expired:
            del self._seen_events[k]
        if key in self._seen_events:
            return True
        self._seen_events[key] = now
        return False

    def _handle_reaction(self, event, client, say):
        """Handle reaction_added event - routes to appropriate handler"""
        # 스킨톤 변형 정규화: "+1::skin-tone-2" → "+1"
        reaction = (event.get("reaction") or "").split("::")[0]

        if reaction not in self._reaction_handlers:
            return

        channel_id = event.get("item", {}).get("channel")
        event_ts = event.get("event_ts") or event.get("ts", "")
        dedup_key = f"{reaction}:{channel_id}:{event_ts}"

        if self._is_duplicate(dedup_key):
            logger.warning(f"[SlackRouter] Duplicate event skipped: {dedup_key}")
            return

        # Check allowed channels
        allowed = self._reaction_channels.get(reaction)
        if allowed and channel_id not in allowed:
            logger.debug(f"[SlackRouter] Reaction '{reaction}' ignored: channel {channel_id} not allowed")
            return

        logger.info(f"[SlackRouter] Reaction '{reaction}' triggered in channel {channel_id}")

        try:
            handler = self._reaction_handlers[reaction]
            handler(event, client, say)
        except Exception as e:
            logger.error(f"[SlackRouter] Error in reaction '{reaction}': {e}")

    def _show_help(self, client, channel_id: str, user_id: str):
        """Show help message (ephemeral - only visible to user)"""
        help_text = get_help_text(self.command)
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=help_text)
