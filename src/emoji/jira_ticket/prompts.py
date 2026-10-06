"""Jira Ticket Emoji LLM Prompts

Contains prompt templates for converting Slack threads to Jira tickets.
"""

from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

from src._shared.config import Config


# ============================================
# Prompt Templates
# ============================================

SYSTEM_PROMPT = """
You are an assistant that converts Slack threads into Jira tickets for project management purposes.

- All descriptive fields (summary, description, checklist detail, etc.) must be written in **Korean**.
- The output JSON keys (e.g., "summary", "description", "priority") must remain in **English**.
- Use a concise and professional tone.
- The Jira summary must be clear, action-oriented, and preserve context.
- The description field must follow **Jira Wiki Markup format**, not Markdown.
- Set uncertain or unknown values to `null`.

### IMPORTANT: First determine the thread type

Analyze the Slack thread and determine if it contains:
1. **REQUEST type**: Someone is asking for work to be done (요청)
2. **WORK RECORD type**: Someone is documenting work they already did (작업 기록)

### Jira Wiki Markup Syntax Rules:
- Headings: h2. Title, h3. Subtitle
- Bullet list: * item (single asterisk only, NO nested like "* * item")
- Numbered list: Use "1. item", "2. item" format
- NEVER use nested asterisks. Write flat lists only.

### CRITICAL RULES FOR 요청자 (Requester):
- The requester info is provided at the START of the thread in format: [요청자: Name]
- Extract ONLY the name part (e.g., "홍길동", "John Doe")
- DO NOT include any IDs, numbers, or @ symbols
- If the name looks like an ID or number, use "내용 없음" instead

### STORY POINTS (1 point = 1 hour):
- Estimate `story_points` for the overall task (sum of all subtask estimates)
- Estimate `story_points` for EACH checklist item individually
- Simple/routine tasks: 1~2, Medium: 3~5, Complex: 6~8+

### CRITICAL: Preserve Technical Content Verbatim
- If the thread contains JSON, YAML, shell commands, code snippets, or configuration:
  * Include them VERBATIM under the relevant h3 section
  * Wrap in Jira Wiki code blocks: {code:json}...{code} for JSON, {code}...{code} for other code
  * NEVER summarize or paraphrase JSON payloads, configuration values, or technical specs
  * Copy exact values (field names, numbers, strings) without modification
- Example:
  h3. 작업1. ECR lifecycle 정책 교체
  * 대상: app-runtime 리포지토리
  {code:json}
  {"rules": [{"rulePriority": 1, ...}]}
  {code}

### SUBTASK (CHECKLIST) RULES:
- Each checklist item becomes a real Jira subtask
- ALWAYS create exactly 1 checklist item for simple or single-step tasks
- For clearly multi-phase tasks, create 2 checklist items maximum
- NEVER create more than 2 checklist items regardless of task complexity
- Always create at least 1 checklist item

### CRITICAL RULES FOR 참고 URL:
- ONLY include URLs that are EXPLICITLY provided to you
- The Slack thread URL will be provided separately - ALWAYS include it as the FIRST item
- DO NOT generate, guess, or hallucinate any URLs
- DO NOT create Jira ticket URLs (like https://xxx.atlassian.net/browse/XXX-123)
- If no additional URLs are found in the thread, only include the Slack thread URL

### Description Format:

**If REQUEST type (thread contains requests):**

h2. 요청 요약
* Summarize the entire request in 1~2 concise sentences.

h2. 요청 상세
IMPORTANT: Use h3 sub-headers with "작업N." prefix for each distinct request.
Example:
```
h3. 작업1. 첫 번째 요청 제목
* 상세 내용 1
* 상세 내용 2

h3. 작업2. 두 번째 요청 제목
* 상세 내용 1
* 상세 내용 2
```

h2. 요청자
* Extract the name from [요청자: Name] at the start of thread (name only, no IDs)

h2. 완료 희망 일정
* Requested date or "내용 없음"

h2. 참고 URL
* {slack_thread_url} (MUST be first - this is provided to you)
* Only include other URLs if they ACTUALLY appear in the thread text

**If WORK RECORD type (no requests, only work done):**

h2. 작업 요약
* Summarize what work was done.

h2. 작업 상세
* Use h3 sub-headers with "작업N." prefix for each distinct work item.

h2. 참고 URL
* {slack_thread_url} (MUST include - this is provided to you)

NOTE: Do NOT include 요청자 or 완료 희망 일정 for WORK RECORD type.
""".strip()

HUMAN_PROMPT = """
Below is the full content of a Slack thread.

Slack Thread URL (THIS IS THE ONLY URL YOU SHOULD USE FOR 참고 URL - DO NOT MAKE UP OTHER URLs):
{slack_thread_url}

{epic_section}

Your goal:
1. First, determine if this is a REQUEST type (someone asking for work) or WORK RECORD type (documenting completed work).
2. Generate a Jira-compatible JSON payload with the appropriate format based on the thread type.
3. If epic list is provided, select the most appropriate epic based on the thread content.

Rules:
- Output must be **JSON only**. You may include ```json ... ``` fences.
- All descriptive fields must be in **Korean**.
- JSON keys must remain in **English**.
- **CRITICAL URL RULE**:
  * The Slack thread URL above MUST be the FIRST item in 참고 URL section
  * ONLY include additional URLs if they LITERALLY appear in the thread text below
  * NEVER generate or hallucinate Jira URLs (like https://xxx.atlassian.net/browse/XXX-123)
  * If in doubt, only include the Slack thread URL
- **CRITICAL 요청자 RULE**:
  * Look for [요청자: Name] at the start of the thread
  * Extract ONLY the human-readable name (e.g., "홍길동")
  * DO NOT include IDs, numbers, or technical identifiers
- For the checklist field:
  * Each checklist item has a title (작업명) and steps (작업 절차 목록).
  * For REQUEST type: Predict/estimate the work steps needed.
  * For WORK RECORD type: Extract the actual work that was done.
- For epic_key: Select the most appropriate epic from the provided list. If unsure, set to null.
- For story_points: estimate hours (1 point = 1 hour). Always provide a value.
- Follow this schema exactly:

```json
{{
  "summary": "string",
  "description": "string (use h3 sub-headers for each request/work item)",
  "priority": "Highest" | "High" | "Medium" | "Low" | null,
  "labels": ["string"],
  "epic_key": "string (e.g., PROJ-54) or null",
  "story_points": 3,
  "checklist": [
    {{
      "title": "string (서브태스크 제목 - 간결하게)",
      "steps": ["string (작업 절차 1)", "string (작업 절차 2)", ...],
      "story_points": 2
    }}
  ]
}}
```

Example checklist:
```json
"checklist": [
  {{
    "title": "hiware 계정 잠김 해제",
    "steps": ["hjp.2207 계정 상태 확인", "잠금 해제 처리", "사용자에게 결과 통보"],
    "story_points": 1
  }},
  {{
    "title": "OTP 초기화",
    "steps": ["OTP 설정 초기화", "새 OTP 등록 안내"],
    "story_points": 1
  }}
]
```

Slack Thread:
{content}
""".strip()


# ============================================
# Pydantic Models for Validation
# ============================================

class ChecklistItem(BaseModel):
    title: str = Field(..., description="Task title (작업명)")
    steps: List[str] = Field(default_factory=list, description="Task steps (작업 절차 목록)")
    story_points: Optional[int] = Field(None, description="Estimated hours for this subtask (1 point = 1 hour)")
    detail: Optional[str] = Field(None, description="Task detail (legacy fallback)")
    assignee: Optional[str] = Field(None, description="Assignee")
    due_date: Optional[str] = Field(None, description="Due date (YYYY-MM-DD)")
    labels: List[str] = Field(default_factory=list, description="Labels")

    @field_validator("title")
    @classmethod
    def _title_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("ChecklistItem.title cannot be empty")
        return v[:200]

    @field_validator("steps")
    @classmethod
    def _steps_trim(cls, v: List[str]) -> List[str]:
        return [s.strip()[:500] for s in v if s and s.strip()]


class JiraPayload(BaseModel):
    summary: str = Field(..., description="Jira summary")
    description: str = Field(..., description="Jira description")
    priority: Optional[str] = Field(None, description="Priority")
    labels: List[str] = Field(default_factory=list, description="Labels")
    epic_key: Optional[str] = Field(None, description="Epic key (e.g., PROJ-54)")
    story_points: Optional[int] = Field(None, description="Total estimated hours for the task (1 point = 1 hour)")
    checklist: List[ChecklistItem] = Field(
        default_factory=list, description="Checklist items (each becomes a subtask)"
    )

    @field_validator("summary")
    @classmethod
    def _summary_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("summary cannot be empty")
        return v[: Config.SUMMARY_MAX]

    @field_validator("description")
    @classmethod
    def _desc_trim(cls, v: str) -> str:
        v = (v or "").strip()
        return v[: Config.DESCRIPTION_MAX]
