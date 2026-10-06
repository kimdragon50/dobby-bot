"""RAG Query Handler - /dobby ask <질문> or /dobby askpv <질문>"""
import threading

from slack_bolt import App

from src._shared.config import Config
from src._shared.logger import logger
from .service import RAGService


class RAGQueryHandler:
    """Slack RAG 명령어 핸들러 (router의 subcommand로 동작)"""

    def __init__(self, app: App):
        self.app = app
        self.rag = RAGService()
        self.command = Config.SLACK_COMMAND  # /dobby or /dobby-test

    def handle_subcommand(self, command: dict, client):
        """서브커맨드 처리 (라우터에서 호출)"""
        text = command.get("text", "").strip()
        user_id = command.get("user_id", "")
        channel_id = command.get("channel_id", "")
        channel_name = command.get("channel_name", "")

        # DM 여부 확인 (channel_name이 'directmessage'인 경우)
        is_dm = channel_name == "directmessage"

        # "ask " 또는 "askpv " 접두사 제거 및 ephemeral 결정
        ephemeral = False
        if text.lower().startswith("askpv "):
            ephemeral = True
            question = text[6:].strip()
        elif text.lower().startswith("ask "):
            question = text[4:].strip()
        else:
            question = text.strip()

        if not question:
            self._send_message(
                client, channel_id, user_id,
                f"질문을 입력해주세요. 예: `{self.command} ask AWS 계정 관리 문서 링크 어디있어?`",
                ephemeral=True, is_dm=is_dm
            )
            return

        logger.info(f"RAG query from {user_id}: {question} (ephemeral={ephemeral}, dm={is_dm})")

        # 처리 중 메시지
        self._send_message(
            client, channel_id, user_id,
            f"_'{question}' 에 대한 답변을 생성 중입니다..._",
            ephemeral=True, is_dm=is_dm
        )

        # 백그라운드에서 RAG 처리
        thread = threading.Thread(
            target=self._process_query,
            args=(question, user_id, channel_id, client, ephemeral, is_dm),
        )
        thread.start()

    def _send_message(self, client, channel_id: str, user_id: str, text: str,
                      ephemeral: bool = False, is_dm: bool = False, blocks: list = None):
        """메시지 전송 (DM에서는 conversations.open 후 postMessage 사용)"""
        try:
            if is_dm:
                # DM에서는 conversations.open으로 채널 열고 일반 메시지 전송
                dm_response = client.conversations_open(users=[user_id])
                dm_channel = dm_response["channel"]["id"]
                if blocks:
                    client.chat_postMessage(channel=dm_channel, text=text, blocks=blocks)
                else:
                    client.chat_postMessage(channel=dm_channel, text=text)
            elif ephemeral:
                if blocks:
                    client.chat_postEphemeral(channel=channel_id, user=user_id, text=text, blocks=blocks)
                else:
                    client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)
            else:
                if blocks:
                    client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
                else:
                    client.chat_postMessage(channel=channel_id, text=text)
        except Exception as e:
            logger.error(f"Failed to send message: {e}")

    def _process_query(self, question: str, user_id: str, channel_id: str, client, ephemeral: bool = False, is_dm: bool = False):
        """백그라운드에서 RAG 쿼리 처리"""
        try:
            answer, sources = self.rag.query_with_sources(question)
            blocks = self._build_response_blocks(question, answer, sources, user_id, ephemeral or is_dm)

            self._send_message(
                client, channel_id, user_id,
                text=f"{question}에 대한 답변",
                ephemeral=ephemeral, is_dm=is_dm, blocks=blocks
            )
        except Exception as e:
            logger.error(f"Error processing RAG query: {e}")
            error_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: <@{user_id}> 질문 처리 중 오류가 발생했습니다.\n```{str(e)[:200]}```"
                    }
                }
            ]
            self._send_message(
                client, channel_id, user_id,
                text="질문 처리 중 오류 발생",
                ephemeral=ephemeral, is_dm=is_dm, blocks=error_blocks
            )

    def _build_response_blocks(self, question: str, answer: str, sources: list, user_id: str, ephemeral: bool = False) -> list:
        """Slack Block Kit 형식의 응답 생성"""
        header_text = "(나에게만 표시) 문서 검색 결과" if ephemeral else "문서 검색 결과"
        blocks = [
            # 헤더
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":mag: {header_text}",
                    "emoji": True
                }
            },
            # 질문
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":bust_in_silhouette: <@{user_id}>  |  :speech_balloon: _{question}_"
                    }
                ]
            },
            {"type": "divider"},
            # 답변
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": answer[:2900]  # Slack 제한
                }
            },
        ]

        # 참고 문서 링크
        if sources:
            blocks.append({"type": "divider"})

            # 폴더와 파일 분리
            folders = [(t, u) for t, u in sources if t.startswith("📁")]
            files = [(t, u) for t, u in sources if t.startswith("📄")]

            # 폴더 섹션
            if folders:
                # 중복 제거
                seen = set()
                unique_folders = []
                for t, u in folders:
                    if u not in seen:
                        seen.add(u)
                        unique_folders.append((t, u))

                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "*:file_folder: 관련 폴더*"}]
                })
                folder_text = "\n".join(f"• <{url}|{title.replace('📁 ', '')}>" for title, url in unique_folders[:3])
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": folder_text}
                })

            # 파일 섹션
            if files:
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "*:page_facing_up: 관련 문서*"}]
                })
                file_text = "\n".join(f"• <{url}|{title.replace('📄 ', '')}>" for title, url in files[:5])
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": file_text}
                })

        return blocks
