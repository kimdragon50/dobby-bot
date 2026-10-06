"""데일리 스크럼 Slack Block Kit 구성

알림 메시지 / 작성 모달 / 개인 답글 / 종합 결과의 레이아웃을 한곳에 모은다.
Slack 에서 읽기 좋도록 섹션·구분선·컨텍스트를 적절히 섞는다.
"""
from datetime import datetime

# Slack action_id / callback_id — handler 와 공유한다
ACTION_OPEN_MODAL = "scrum_open_modal"
ACTION_SUMMARIZE = "scrum_summarize"
CALLBACK_SUBMIT = "scrum_submit_modal"

# 모달 입력 필드 (block_id, action_id)
FIELDS = [
    ("yesterday", "어제 한 일", "어제 완료했거나 진행한 작업을 적어주세요", True),
    ("today", "오늘 할 일", "오늘 집중할 작업을 적어주세요", True),
    ("blocker", "이슈 / 공유사항", "막힌 부분이나 공유할 내용 (없으면 비워두세요)", False),
]

_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def format_date(dt: datetime) -> str:
    """2026년 9월 28일 (월) 형식."""
    return f"{dt.year}년 {dt.month}월 {dt.day}일 ({_WEEKDAY_KR[dt.weekday()]})"


def copy_template() -> str:
    """복붙용 템플릿 — 모달 대신 직접 쓰고 싶은 사람을 위한 것."""
    return (
        "• 어제 한 일:\n"
        "• 오늘 할 일:\n"
        "• 이슈/공유사항:"
    )


def announcement_blocks(date_label: str, mention_channel: bool = True) -> list:
    """스케줄 알림 본문.

    @channel 은 blocks 안에서도 동작하지만, 알림 신뢰성을 위해 호출측에서
    text 파라미터에도 동일 문구를 넣는다.
    """
    head = "<!channel> " if mention_channel else ""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🗓️  데일리 스크럼 — {date_label}", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{head}좋은 아침입니다! 오늘의 스크럼을 *이 스레드에* 남겨주세요 🧵\n"
                    "아래 *스크럼 작성하기* 버튼을 누르면 입력창이 열립니다.\n"
                    "_고칠 내용이 생기면 같은 버튼을 다시 누르세요 — 쓴 내용이 채워진 채로 열리고, 등록하면 기존 글이 수정됩니다._"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✍️  스크럼 작성하기", "emoji": True},
                    "style": "primary",
                    "action_id": ACTION_OPEN_MODAL,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📋  전체 종합 정리", "emoji": True},
                    "action_id": ACTION_SUMMARIZE,
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "_직접 작성하시려면 아래 템플릿을 복사해 스레드에 답글로 남겨주세요._\n"
                    f"```{copy_template()}```"
                ),
            },
        },
    ]


# 개인 스크럼 메시지에 원본 입력값을 실어두는 Slack message metadata.
# 별도 저장소 없이 "모달 다시 열기 → 기존 값 채우기 → 같은 글 수정" 이 가능해진다.
# (봇이 올린 글이라 사용자가 Slack UI 로 직접 수정할 수 없다)
ENTRY_EVENT_TYPE = "scrum_entry"


def entry_metadata(user_id: str, values: dict) -> dict:
    payload = {"user": user_id}
    for key, _, _, _ in FIELDS:
        payload[key] = (values.get(key) or "")[:2900]  # metadata 크기 제한 여유
    return {"event_type": ENTRY_EVENT_TYPE, "event_payload": payload}


def submit_modal(private_metadata: str, values: dict = None, editing: bool = False) -> dict:
    """스크럼 작성 모달.

    private_metadata 에 채널/스레드/수정 대상 ts 를 실어 보낸다.
    values 가 있으면 기존 입력을 채워 수정 모드로 연다.
    """
    values = values or {}
    guide = (
        "이미 작성한 내용을 불러왔습니다. 수정 후 등록하면 기존 글이 갱신됩니다."
        if editing
        else "작성하면 스크럼 스레드에 자동으로 정리되어 올라갑니다."
    )
    blocks = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": guide}]}
    ]
    for key, label, placeholder, required in FIELDS:
        element = {
            "type": "plain_text_input",
            "action_id": f"{key}_input",
            "multiline": True,
            "placeholder": {"type": "plain_text", "text": placeholder},
        }
        prev = (values.get(key) or "").strip()
        if prev:
            element["initial_value"] = prev
        blocks.append(
            {
                "type": "input",
                "block_id": f"{key}_block",
                "optional": not required,
                "label": {"type": "plain_text", "text": label},
                "element": element,
            }
        )
    return {
        "type": "modal",
        "callback_id": CALLBACK_SUBMIT,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "데일리 스크럼"},
        "submit": {"type": "plain_text", "text": "수정" if editing else "등록"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": blocks,
    }


def entry_blocks(user_id: str, values: dict) -> list:
    """개인 스크럼 답글 — 항목별로 보기 좋게 정리.

    맨 앞에 divider 를 둔다. Slack 은 같은 발신자(도비)의 연속 메시지를
    하나로 묶어 보여주기 때문에, 구분선이 없으면 여러 사람의 스크럼이
    이어 붙어 어디서 끊기는지 알 수 없다.
    """
    lines = [f"*<@{user_id}>님의 스크럼*"]
    icons = {"yesterday": "✅", "today": "🎯", "blocker": "⚠️"}
    labels = {k: label for k, label, _, _ in FIELDS}
    for key, _, _, _ in FIELDS:
        text = (values.get(key) or "").strip()
        if not text:
            continue
        body = "\n".join(f"    {ln}" for ln in text.splitlines() if ln.strip())
        lines.append(f"\n{icons[key]} *{labels[key]}*\n{body}")
    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]


def summary_blocks(date_label: str, participant_count: int, summary_md: str) -> list:
    """종합 정리 결과. 개인 스크럼과 마찬가지로 구분선으로 시작한다."""
    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📋  *데일리 스크럼 종합 — {date_label}*",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"참여 {participant_count}명 · 도비가 정리했습니다"}
            ],
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_md}},
    ]
