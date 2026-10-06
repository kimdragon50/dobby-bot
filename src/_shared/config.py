"""Core configuration for Dobby Bot - Common settings only"""
import os
from pathlib import Path
from typing import Dict, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # project root
ENV = os.getenv("APP_ENV", "development")


class Config:
    """Core configuration class - common settings shared across all features"""

    # ============================================
    # Application Configuration
    # ============================================
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # ============================================
    # Slack Configuration
    # ============================================
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")
    SLACK_SIGNING_SECRET: str = os.getenv("SLACK_SIGNING_SECRET", "")
    SLACK_COMMAND: str = os.getenv("SLACK_COMMAND", "/dobby")  # test: /dobby-test
    # 도움말 하단에 노출할 문의 채널 ID (선택, e.g. C0123456789)
    SUPPORT_CHANNEL_ID: str = os.getenv("SUPPORT_CHANNEL_ID", "")

    # ============================================
    # Jira Configuration
    # ============================================
    JIRA_ENABLED: bool = os.getenv("JIRA_ENABLED", "true").lower() == "true"
    JIRA_SERVER_URL: str = os.getenv("JIRA_SERVER_URL", "")
    JIRA_USERNAME: str = os.getenv("JIRA_USERNAME", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")

    # ============================================
    # AWS Bedrock Configuration
    # ============================================
    AWS_REGION: str = os.getenv("AWS_REGION", "ap-northeast-2")
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    BEDROCK_MODEL_ID: str = os.getenv(
        "BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20240620-v1:0"
    )
    TEMPERATURE: float = float(os.getenv("TEMPERATURE", "0.5"))
    MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4000"))

    # Bedrock Knowledge Base (RAG)
    KNOWLEDGE_BASE_ID: str = os.getenv("KNOWLEDGE_BASE_ID", "")

    # ============================================
    # Naver Search API Configuration
    # ============================================
    NAVER_CLIENT_ID: str = os.getenv("NAVER_CLIENT_ID", "")
    NAVER_CLIENT_SECRET: str = os.getenv("NAVER_CLIENT_SECRET", "")

    # ============================================
    # Google Calendar Configuration
    # ============================================
    GOOGLE_SERVICE_ACCOUNT_FILE: str = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/service-account.json"
    )

    # ============================================
    # AgentCore Supervisor (@dobby mention / DM)
    # ============================================
    # Bedrock AgentCore Runtime ARN (supervisor agent)
    # 비워두면 멘션/DM 핸들러는 동작하지 않는다.
    AGENTCORE_SUPERVISOR_ARN: str = os.getenv("AGENTCORE_SUPERVISOR_ARN", "")

    # ============================================
    # Alert Analyze Configuration (:op_analyze:)
    # ============================================
    KUBECONFIG_PATH: str = os.getenv("KUBECONFIG_PATH", "")
    # Observability endpoints (비워두면 해당 수집기는 건너뛴다)
    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "")  # e.g. http://prometheus.example.com
    LOKI_URL: str = os.getenv("LOKI_URL", "")  # e.g. http://loki-gateway.example.com
    # 알림 분석 Jira 티켓 생성 / :op_sre_complete: 완료 처리 대상 프로젝트
    OPS_JIRA_PROJECT_KEY: str = os.getenv("OPS_JIRA_PROJECT_KEY", "SRE")
    CONFLUENCE_SRE_SPACE_KEY: str = os.getenv("CONFLUENCE_SRE_SPACE_KEY", "")
    CONFLUENCE_ALERT_PARENT_PAGE_ID: str = os.getenv("CONFLUENCE_ALERT_PARENT_PAGE_ID", "")

    # ============================================
    # Langfuse (사용자 피드백 score 기록)
    # ============================================
    # 3종 모두 설정된 경우에만 활성화 — 미설정 시 피드백 기록은 no-op
    LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "")
    LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")

    # 👎 사유 대기 상태 DynamoDB 테이블 (PK: thread_key, TTL attr: ttl)
    FEEDBACK_PENDING_TABLE: str = os.getenv("FEEDBACK_PENDING_TABLE", "dobby-feedback-pending")

    # ============================================
    # Daily Scrum (데일리 스크럼 알림)
    # ============================================
    # 자동 게시 채널은 DynamoDB 로 관리한다 (/dobby scrum on|off|list).
    # 환경변수로 두면 채널을 바꿀 때마다 재배포해야 한다.
    SCRUM_CHANNEL_TABLE: str = os.getenv("SCRUM_CHANNEL_TABLE", "dobby-scrum-channels")
    SCRUM_HOUR: int = int(os.getenv("SCRUM_HOUR", "9"))
    SCRUM_MINUTE: int = int(os.getenv("SCRUM_MINUTE", "0"))
    # APScheduler cron 표기 — 기본값은 평일(월~금)
    SCRUM_DAYS: str = os.getenv("SCRUM_DAYS", "mon-fri")

    # 가장 최근 스프린트 티켓만 표시한다 (활성 스프린트, 없으면 직전 종료분).
    # false 로 두면 JQL 결과 전량을 표시한다 — 스프린트를 안 쓰는 팀용.
    SCRUM_SPRINT_ONLY: bool = os.getenv("SCRUM_SPRINT_ONLY", "true").lower() == "true"

    # 스크럼 스레드에 함께 붙일 Jira 티켓 현황 JQL.
    # 비우면 티켓 현황을 조회하지 않는다 (스크럼 알림은 그대로 동작).
    # e.g. project = PROJ AND (key in (PROJ-100) OR issue in portfolioChildIssuesOf("PROJ-100"))
    SCRUM_JIRA_JQL: str = os.getenv("SCRUM_JIRA_JQL", "")

    # ============================================
    # Weekly Report
    # ============================================
    # /dobby wkreport 모달에 노출할 Jira 그룹 이름 접두사
    JIRA_GROUP_PREFIX: str = os.getenv("JIRA_GROUP_PREFIX", "team")

    # ============================================
    # LLM Limits (shared across features)
    # ============================================
    INPUT_CHAR_LIMIT: int = 15000
    SUMMARY_MAX: int = 120
    DESCRIPTION_MAX: int = 50000
    LABEL_MAX_LEN: int = 40
    HEAD_TAIL_KEEP_RATIO: float = 0.8

    # Retry settings
    MAX_RETRY_ATTEMPTS: int = 10
    INITIAL_BACKOFF_SECONDS: int = 1
    MAX_BACKOFF_SECONDS: int = 60

    # HTTP timeout settings (connect, read) in seconds
    HTTP_CONNECT_TIMEOUT: int = 10
    HTTP_READ_TIMEOUT: int = 30

    @classmethod
    def get_http_timeout(cls) -> tuple:
        """Get HTTP timeout tuple (connect, read)"""
        return (cls.HTTP_CONNECT_TIMEOUT, cls.HTTP_READ_TIMEOUT)

    @classmethod
    def validate_slack(cls) -> bool:
        """Validate Slack configuration"""
        missing = []
        if not cls.SLACK_BOT_TOKEN:
            missing.append("SLACK_BOT_TOKEN")
        if not cls.SLACK_APP_TOKEN:
            missing.append("SLACK_APP_TOKEN")
        if not cls.SLACK_SIGNING_SECRET:
            missing.append("SLACK_SIGNING_SECRET")

        if missing:
            raise ValueError(f"Missing required Slack configuration: {', '.join(missing)}")
        return True

    @classmethod
    def validate_jira(cls) -> bool:
        """Validate Jira configuration"""
        if not cls.JIRA_ENABLED:
            return True

        missing = []
        if not cls.JIRA_SERVER_URL:
            missing.append("JIRA_SERVER_URL")
        if not cls.JIRA_USERNAME:
            missing.append("JIRA_USERNAME")
        if not cls.JIRA_API_TOKEN:
            missing.append("JIRA_API_TOKEN")

        if missing:
            raise ValueError(f"Missing required Jira configuration: {', '.join(missing)}")
        return True

    @classmethod
    def validate_aws(cls) -> bool:
        """Validate AWS configuration"""
        missing = []
        if not cls.AWS_ACCESS_KEY_ID:
            missing.append("AWS_ACCESS_KEY_ID")
        if not cls.AWS_SECRET_ACCESS_KEY:
            missing.append("AWS_SECRET_ACCESS_KEY")

        if missing:
            raise ValueError(f"Missing required AWS configuration: {', '.join(missing)}")
        return True

    @classmethod
    def validate_all(cls) -> bool:
        """Validate all required configuration"""
        cls.validate_slack()
        cls.validate_jira()
        cls.validate_aws()
        return True

