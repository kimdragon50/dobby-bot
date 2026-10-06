#!/usr/bin/env python3
"""
Dobby Bot - 업무 자동화 Slack 봇

트리거 방식별 기능:

슬래시 명령어 (command/)
- jira_ticket:        /dobby jira      → Jira 티켓 생성 모달
- weekly_report:      /dobby wkreport  → 주간 리포트
- rag_query:          /dobby ask|askpv → Knowledge Base 질의
- food_recommendation:/dobby food|bob  → 메뉴 추천
- calendar_briefing:  /dobby briefing  → 일정 브리핑 등록 (스케줄 DM)
- daily_scrum:        /dobby scrum     → 데일리 스크럼 알림 (스케줄 + 모달 + 종합)

이모지 리액션 (emoji/)
- jira_ticket:    스레드 → LLM 분석 → Jira 티켓
- complete_ticket / alert_analyze

멘션·DM (mention/)
- @dobby 멘션 또는 DM → AgentCore Supervisor → 도메인 코디네이터 위임

피드백 (feedback/)
- 봇 답변의 👍/👎 리액션 → Langfuse user-feedback score (👎는 사유 수집)
"""
import atexit
import signal
import sys
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src._shared.config import Config
from src._shared.health import start_health_server
from src._shared.logger import logger
from src._shared.router import SlackRouter
from src._shared.jira import JiraService
from src._shared.llm import LLMService
from src.command.jira_ticket import JiraModalHandler
from src.command.weekly_report import WeeklyReportHandler
from src.command.rag_query import RAGQueryHandler
from src.command.food_recommendation import FoodRecommendationHandler
from src.command.calendar_briefing import CalendarBriefingHandler
from src.command.daily_scrum import DailyScrumHandler, ScrumScheduler
from src.emoji.jira_ticket import ReactionHandler, ReactionConfig
from src.emoji.complete_ticket import CompleteTicketHandler
from src.emoji.alert_analyze import AlertAnalyzeHandler
from src.mention import MentionHandler
from src.feedback import FeedbackHandler, FEEDBACK_REACTIONS


# Global references for cleanup
slack_handler = None
app = None


def cleanup():
    """Cleanup on process exit"""
    global slack_handler
    if slack_handler:
        logger.info("🧹 Cleaning up...")
        try:
            slack_handler.close()
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")
        slack_handler = None


atexit.register(cleanup)


def signal_handler(signum, frame):
    """Handle termination signals"""
    logger.info(f"📥 Received signal {signum}, shutting down...")
    sys.exit(0)


def main():
    """Main entry point"""
    global slack_handler, app

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Start health server immediately so readiness probes pass during startup
    start_health_server(lambda: slack_handler)

    logger.info("=" * 60)
    logger.info("🧙 Dobby Bot Starting...")
    logger.info(f"   Environment: {Config.APP_ENV}")
    logger.info("=" * 60)

    # Validate configuration
    try:
        Config.validate_slack()
        logger.info("✓ Slack configuration validated")

        if Config.JIRA_ENABLED:
            Config.validate_jira()
            logger.info("✓ Jira configuration validated")

        Config.validate_aws()
        logger.info("✓ AWS configuration validated")
    except ValueError as e:
        logger.error(f"✗ Configuration error: {e}")
        logger.error("\nPlease check your .env file or environment variables")
        return 1

    # Initialize Slack app
    app = App(
        token=Config.SLACK_BOT_TOKEN,
        signing_secret=Config.SLACK_SIGNING_SECRET,
    )
    logger.info("✓ Slack App initialized")

    # Initialize services
    try:
        jira_service = JiraService()
        logger.info("✓ Jira service initialized")
    except Exception as e:
        logger.error(f"✗ Failed to initialize Jira service: {e}")
        jira_service = None

    try:
        llm_service = LLMService()
        logger.info("✓ LLM service initialized")
    except Exception as e:
        logger.error(f"✗ Failed to initialize LLM service: {e}")
        llm_service = None

    # Initialize unified router
    router = SlackRouter(app)

    # Register handlers
    handlers_registered = []

    # 1. Jira Ticket - Modal handler (shortcut → Jira modal)
    jira_modal_handler = None
    if jira_service and llm_service:
        jira_modal_handler = JiraModalHandler(app, jira_service, llm_service)
        jira_modal_handler.register_handlers()
        router.register_command('jira', jira_modal_handler.open_jira_modal)
        handlers_registered.append("Jira Modal (/dobby jira, shortcut)")

    # 2. Weekly Report handler (wkreport subcommand)
    if jira_service and llm_service:
        weekly_handler = WeeklyReportHandler(app, jira_service, llm_service)
        weekly_handler.register_handlers()
        router.register_command('wkreport', weekly_handler.handle_subcommand)
        handlers_registered.append("Weekly Report (/dobby wkreport)")

    # 3. RAG Query handler (ask, askpv subcommands)
    if Config.KNOWLEDGE_BASE_ID:
        rag_handler = RAGQueryHandler(app)
        router.register_command('ask', rag_handler.handle_subcommand)
        router.register_command('askpv', rag_handler.handle_subcommand)
        handlers_registered.append(f"RAG Query ({Config.SLACK_COMMAND} ask/askpv)")

    # 5. Food Recommendation handler
    if llm_service:
        food_handler = FoodRecommendationHandler(app, llm_service)
        food_handler.register_handlers()
        router.register_command('food', food_handler.handle_subcommand)
        router.register_command('bob', food_handler.handle_subcommand)
        handlers_registered.append(f"Food Recommendation ({Config.SLACK_COMMAND} food/bob)")

    # 6. Calendar Briefing handler
    briefing_handler = None
    if llm_service:
        briefing_handler = CalendarBriefingHandler(app, llm_service)
        briefing_handler.register_handlers()
        router.register_command('briefing', briefing_handler.handle_subcommand)
        handlers_registered.append(f"Calendar Briefing ({Config.SLACK_COMMAND} briefing)")

    # 7. Daily Scrum handler (스케줄 알림 + 작성 모달 + 종합 정리)
    scrum_handler = None
    if llm_service:
        scrum_handler = DailyScrumHandler(app, llm_service, jira_service)
        scrum_handler.register_handlers()
        router.register_command('scrum', scrum_handler.handle_subcommand)
        handlers_registered.append(f"Daily Scrum ({Config.SLACK_COMMAND} scrum)")

    # 4. Jira Ticket Emoji - Reaction handler (reaction → Jira via LLM)
    if jira_service and llm_service:
        reaction_handler = ReactionHandler(jira_service, llm_service)
        reaction_mapping = ReactionConfig.get_reaction_mapping()
        allowed_channels = ReactionConfig.get_allowed_channels()
        for reaction_name in reaction_mapping.keys():
            router.register_reaction(
                reaction_name,
                reaction_handler.handle_reaction_added,
                allowed_channels if allowed_channels else None
            )
        handlers_registered.append(f"Jira Ticket Emoji ({', '.join(reaction_mapping.keys())})")

    # 5. Complete Ticket Emoji - marks linked Jira tickets as done
    if jira_service:
        complete_handler = CompleteTicketHandler(jira_service)
        allowed_channels = ReactionConfig.get_allowed_channels()
        router.register_reaction(
            CompleteTicketHandler.REACTION_NAME,
            complete_handler.handle_reaction_added,
            allowed_channels if allowed_channels else None
        )
        handlers_registered.append(f"Complete Ticket (:{CompleteTicketHandler.REACTION_NAME}:)")

    # 7. Alert Analyze Emoji - analyzes Grafana alerts (:op_analyze:)
    if jira_service and llm_service:
        alert_handler = AlertAnalyzeHandler(jira_service, llm_service)
        alert_handler.register_handlers(app)
        router.register_reaction(
            AlertAnalyzeHandler.REACTION_NAME,
            alert_handler.handle_reaction_added,
            None  # all channels
        )
        handlers_registered.append(f"Alert Analyze (:{AlertAnalyzeHandler.REACTION_NAME}:)")

    # 8. Feedback handler (봇 답변의 👍/👎 리액션 → Langfuse score + 👎 사유 수집)
    #    MentionHandler보다 먼저 생성 — DM 사유 답글 인터셉터로 전달해야 함
    feedback_handler = None
    try:
        bot_user_id = app.client.auth_test()["user_id"]
    except Exception as e:
        logger.warning(f"✗ Failed to resolve bot user id (feedback disabled): {e}")
        bot_user_id = ""
    if bot_user_id:
        feedback_handler = FeedbackHandler(bot_user_id)
        feedback_handler.warm()  # 재시작 후 사유 대기 힌트 복원 (best-effort)
        for reaction_name in FEEDBACK_REACTIONS:
            router.register_reaction(reaction_name, feedback_handler.handle_reaction, None)
        # 채널 스레드의 사유 답글 수집 (DM은 MentionHandler 인터셉터 경유)
        app.event("message")(feedback_handler.handle_message)
        handlers_registered.append("Feedback (👍/👎 → Langfuse score)")

    # 9. Mention handler (@dobby mention / DM → AgentCore Supervisor)
    mention_handler = MentionHandler()
    mention_handler.register_handlers(
        app,
        message_interceptors=(
            [feedback_handler.try_consume_reply] if feedback_handler else None
        ),
        on_answer=feedback_handler.seed_reactions if feedback_handler else None,
    )
    handlers_registered.append("Mention (@dobby, DM → AgentCore Supervisor)")

    # Register router (must be called after all handlers are registered)
    router.register()

    logger.info(f"✓ Registered handlers: {', '.join(handlers_registered)}")

    # Start Socket Mode handler
    logger.info("\n" + "=" * 60)
    logger.info("🚀 Dobby Bot is starting!")
    logger.info("=" * 60)
    logger.info("\nAvailable features:")
    for handler in handlers_registered:
        logger.info(f"  • {handler}")
    logger.info("\nPress Ctrl+C to stop\n")

    try:
        slack_handler = SocketModeHandler(app, Config.SLACK_APP_TOKEN)
        slack_handler.connect()

        # Wait for connection to stabilize
        logger.info("⏳ Waiting for connection to stabilize...")
        time.sleep(3)

        # Start calendar briefing scheduler
        if briefing_handler:
            briefing_handler.start_scheduler(app.client)

        # Start daily scrum scheduler (SCRUM_CHANNEL 설정 시에만 동작)
        if scrum_handler:
            ScrumScheduler(app.client, scrum_handler).start()

        logger.info("✅ Dobby Bot is ready!\n")

        # Keep running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("\n👋 Shutting down Dobby Bot...")
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
