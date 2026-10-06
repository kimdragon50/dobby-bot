"""Unified LLM Service for Dobby Bot (AWS Bedrock)

Provides common LLM functionality for all bot features.
Domain-specific prompts should be managed in each domain's handler.
"""

import asyncio
import json
import re
from typing import Dict, Any, List, Optional

import boto3
from botocore.config import Config as BotoConfig

from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser

from src._shared.config import Config
from src._shared.logger import logger


# ============================================
# Utility Functions
# ============================================

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _build_model_kwargs(model_id: str, temperature: float, max_tokens: int) -> dict:
    """Claude 5+ (global.* inference profiles) 은 temperature deprecated."""
    kwargs: dict = {"max_tokens": max_tokens}
    if not model_id.startswith("global."):
        kwargs["temperature"] = temperature
    return kwargs


def extract_json_block(text: str) -> str:
    """Extract JSON from markdown code block"""
    m = _JSON_BLOCK_RE.search(text or "")
    return m.group(1).strip() if m else (text or "").strip()


def fallback_brace_block(text: str) -> Optional[str]:
    """Extract JSON by finding first { and last }"""
    if not text:
        return None
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        return text[s : e + 1]
    return None


def _fix_json_control_chars(s: str) -> str:
    """Escape literal control characters inside JSON string values.

    LLMs sometimes emit real newlines/tabs inside a JSON string field
    (e.g. a code snippet field) instead of \\n / \\t, which makes json.loads fail.
    """
    result = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            result.append("\\r")
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)
    return "".join(result)


def safe_json_parse(s: str) -> Dict[str, Any]:
    """Robust JSON parsing"""
    try:
        return json.loads(s)
    except Exception:
        pass
    block = extract_json_block(s)
    try:
        return json.loads(block)
    except Exception:
        pass
    # LLM emitted literal newlines inside string values — fix and retry
    try:
        return json.loads(_fix_json_control_chars(block or s))
    except Exception:
        pass
    brace = fallback_brace_block(s)
    if brace:
        try:
            return json.loads(brace)
        except Exception:
            return json.loads(_fix_json_control_chars(brace))
    raise json.JSONDecodeError("Unable to parse JSON from model output", s, 0)


def head_tail(text: str, max_len: int, head_ratio: float = 0.8) -> str:
    """Truncate text keeping head and tail"""
    t = text or ""
    if len(t) <= max_len:
        return t
    head_len = int(max_len * head_ratio)
    tail_len = max_len - head_len
    return t[:head_len] + "\n...\n" + t[-tail_len:]


# ============================================
# LLM Service Class
# ============================================


class LLMService:
    """Unified LLM service for all bot features

    Provides common LLM invocation with retry logic.
    Domain-specific prompt handling should be done in each handler.
    """

    _instance: Optional["LLMService"] = None
    _bedrock_client = None
    _llm = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._bedrock_client is None:
            self._initialize_client()

    def _initialize_client(self):
        """Initialize AWS Bedrock client"""
        try:
            Config.validate_aws()

            retry_config = BotoConfig(
                retries={"max_attempts": Config.MAX_RETRY_ATTEMPTS, "mode": "standard"},
                connect_timeout=60,
                read_timeout=300,
            )

            session = boto3.Session(
                aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
                region_name=Config.AWS_REGION,
            )

            self._bedrock_client = session.client(
                "bedrock-runtime", config=retry_config
            )

            self._llm = ChatBedrock(
                client=self._bedrock_client,
                region_name=Config.AWS_REGION,
                model_id=Config.BEDROCK_MODEL_ID,
                model_kwargs=_build_model_kwargs(
                    Config.BEDROCK_MODEL_ID, Config.TEMPERATURE, Config.MAX_TOKENS
                ),
            )

            logger.info(
                f"LLM client initialized: model={Config.BEDROCK_MODEL_ID}, region={Config.AWS_REGION}"
            )

        except Exception as e:
            logger.error(f"Failed to initialize LLM client: {e}")
            raise

    async def _invoke_with_retry(self, chain, input_data) -> str:
        """Invoke Bedrock with exponential backoff retry.

        input_data may be a dict (template variables) or a list of messages.
        """
        attempt = 0
        backoff = Config.INITIAL_BACKOFF_SECONDS
        last_exception = None

        while attempt < Config.MAX_RETRY_ATTEMPTS:
            try:
                logger.info(
                    f"Bedrock API call attempt {attempt + 1}/{Config.MAX_RETRY_ATTEMPTS}"
                )
                result = await chain.ainvoke(input_data)

                if attempt > 0:
                    logger.info(f"Bedrock API call succeeded after {attempt} retries")

                return result

            except Exception as e:
                last_exception = e
                error_msg = str(e)

                # Fatal errors - no retry
                fatal_errors = (
                    "ValidationException",
                    "AccessDeniedException",
                    "ResourceNotFoundException",
                    "InvalidParameterException",
                    "UnauthorizedAccess",
                )

                is_fatal = any(err in error_msg for err in fatal_errors)

                if is_fatal:
                    logger.error(f"Fatal error (no retry): {error_msg}")
                    raise

                # All other errors are retryable
                attempt += 1

                if attempt >= Config.MAX_RETRY_ATTEMPTS:
                    logger.error(f"Max retry attempts reached. Last error: {error_msg}")
                    raise

                logger.warning(
                    f"Bedrock API error (attempt {attempt}): {error_msg[:200]}. Retrying in {backoff}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, Config.MAX_BACKOFF_SECONDS)

        raise (
            last_exception
            if last_exception
            else Exception("Max retry attempts reached")
        )

    # ============================================
    # Generic Invocation Methods
    # ============================================

    async def invoke(
        self,
        system_prompt: str,
        human_prompt: str,
        input_data: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        model_id: Optional[str] = None,
    ) -> str:
        """Invoke the LLM with system + human prompts.

        Uses direct message objects (not ChatPromptTemplate) to avoid
        ValueError when prompt content contains literal curly braces
        from kubectl/Prometheus/log output.
        """
        try:
            sys_content = system_prompt.strip()
            human_content = human_prompt.strip()

            # Apply variable substitution via simple str.replace so that
            # literal Jira Wiki markup like {code:json} or {code} in the prompt
            # template are NOT interpreted as format specs.
            if input_data:
                for key, val in input_data.items():
                    placeholder = "{" + key + "}"
                    sys_content = sys_content.replace(placeholder, str(val))
                    human_content = human_content.replace(placeholder, str(val))

            messages = [
                SystemMessage(content=sys_content),
                HumanMessage(content=human_content),
            ]

            effective_model = model_id or Config.BEDROCK_MODEL_ID
            if model_id or (temperature is not None and temperature != Config.TEMPERATURE):
                llm = ChatBedrock(
                    client=self._bedrock_client,
                    region_name=Config.AWS_REGION,
                    model_id=effective_model,
                    model_kwargs=_build_model_kwargs(
                        effective_model,
                        temperature if temperature is not None else Config.TEMPERATURE,
                        Config.MAX_TOKENS,
                    ),
                )
            else:
                llm = self._llm

            parser = StrOutputParser()
            chain = llm | parser
            result = await self._invoke_with_retry(chain, messages)
            return result.strip()

        except Exception as e:
            logger.error(f"LLM invocation failed: {e}")
            raise

    async def invoke_for_json(
        self,
        system_prompt: str,
        human_prompt: str,
        input_data: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        model_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        LLM invocation that expects JSON response.

        Args:
            system_prompt: System prompt template
            human_prompt: Human prompt template
            input_data: Dictionary of values to fill placeholders
            temperature: Override temperature for this call (None = use default)

        Returns:
            Parsed JSON as dictionary
        """
        result = await self.invoke(system_prompt, human_prompt, input_data, temperature, model_id)
        logger.info(f"LLM response received (sample): {result[:300]}")
        return safe_json_parse(result)

    # ============================================
    # Weekly Report: Epic Summarization
    # ============================================

    async def summarize_epic_tickets(self, epic_name: str, tickets: List[Dict]) -> str:
        """
        Summarize tickets under an epic for weekly report.
        """
        try:
            ticket_info = []
            for t in tickets:
                info = f"- {t['key']}: {t['summary']} (상태: {t['status']})"
                if t.get("description"):
                    desc = t["description"][:200]
                    info += f"\n  설명: {desc}"

                # Add subtasks if present
                subtasks = t.get("subtasks", [])
                if subtasks:
                    info += "\n  서브태스크:"
                    for st in subtasks:
                        status_info = st.get("resolution_date", st.get("status", ""))
                        info += f"\n    - {st['summary']} ({status_info})"

                ticket_info.append(info)

            tickets_text = "\n".join(ticket_info)

            system_prompt = """
You are a technical writer creating weekly status reports for a software development team.

Your task:
- Summarize the work done on tickets under a specific epic
- Write in Korean
- Be concise and highlight key achievements
- Group related work together
- Use the following list hierarchy: Numbered list (1., 2., 3...) for main points, - for sub-points (subtasks)
- For each main ticket, list its subtasks below with completion date (MM/DD format) or "진행 중" status
- Identify any critical/high-priority issues separately

Format example:
1. 메인 작업 제목 (상태 또는 ~예정일)
   - 서브태스크1 (01/22)
   - 서브태스크2 (진행 중)
2. 다른 메인 작업 제목 (~01/31)
   - 서브태스크3 (01/20)
"""

            human_prompt = f"""
에픽: {epic_name}

티켓 목록:
{tickets_text}

위 티켓들의 작업 내용을 주간 보고서 형식으로 요약해주세요.
서브태스크가 있는 경우 메인 티켓 아래에 들여쓰기하여 표시하고, 완료된 서브태스크는 완료일(MM/DD)을, 진행 중인 서브태스크는 "진행 중"을 표시해주세요.
"""

            messages = [
                SystemMessage(content=system_prompt.strip()),
                HumanMessage(content=human_prompt.strip()),
            ]
            chain = self._llm | StrOutputParser()
            result = await self._invoke_with_retry(chain, messages)
            return result.strip()

        except Exception as e:
            logger.error(f"Epic summarization failed: {e}")
            return f"에픽 '{epic_name}' 요약 실패: {str(e)}"

    async def generate_weekly_report(
        self, grouped_tickets: Dict[str, List[Dict]]
    ) -> str:
        """
        Generate a complete weekly report from grouped tickets.
        """
        try:
            report_parts = []

            for epic_name, tickets in grouped_tickets.items():
                summary = await self.summarize_epic_tickets(epic_name, tickets)
                report_parts.append(f"### {epic_name}\n{summary}\n")

            full_report = "\n".join(report_parts)

            # Generate executive summary
            system_prompt = """
You are creating an executive summary for a weekly status report.

Your task:
- Identify critical/high-priority issues
- Summarize key achievements
- Write in Korean
- Be concise (3-5 bullet points max)

Output format:
## 주요 이슈 (경영진 보고용)
- Critical issue 1
- Critical issue 2

## 팀 내부 공유
- Achievement 1
- Achievement 2
"""

            human_prompt = f"""
아래 주간 작업 내용을 바탕으로 경영진 보고용 요약과 팀 내부 공유 내용을 구분해서 작성해주세요.

{full_report}
"""

            messages = [
                SystemMessage(content=system_prompt.strip()),
                HumanMessage(content=human_prompt.strip()),
            ]
            chain = self._llm | StrOutputParser()
            executive_summary = await self._invoke_with_retry(chain, messages)

            final_report = (
                f"{executive_summary.strip()}\n\n---\n\n## 상세 내용\n\n{full_report}"
            )
            return final_report

        except Exception as e:
            logger.error(f"Weekly report generation failed: {e}")
            # Return basic report on error
            basic_report = []
            for epic_name, tickets in grouped_tickets.items():
                ticket_list = "\n".join(
                    [f"  - {t['key']}: {t['summary']}" for t in tickets]
                )
                basic_report.append(f"### {epic_name}\n{ticket_list}")
            return "\n\n".join(basic_report)
