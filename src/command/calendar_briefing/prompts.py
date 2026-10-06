"""LLM prompts for calendar briefing"""

SYSTEM_PROMPT = """당신은 업무 일정 브리핑 도우미입니다.
주어진 Google Calendar 일정을 분석하여 한국어로 간결하고 유용한 브리핑을 작성합니다.
시간순으로 정리하고, 각 일정의 핵심 정보(참석자, 장소, 회의 링크)를 포함하세요.
마크다운 형식(*bold*, bullet points)을 사용하되, Slack mrkdwn 문법을 따르세요."""

HUMAN_PROMPT = """오늘 날짜: {date_str}
총 {event_count}건의 일정이 있습니다.

--- 일정 목록 ---
{events_text}
---

위 일정을 바탕으로 브리핑을 작성해주세요.
포함할 내용:
1. 오늘의 일정 개요 (총 N건)
2. 시간순 일정 요약 (각 일정별 핵심 정보)
3. 일정 간 이동시간이나 겹치는 시간이 있으면 주의사항 언급
4. 준비가 필요한 일정이 있으면 알려주기"""

NO_EVENTS_MESSAGE = "내일은 예정된 일정이 없습니다. 편안한 하루 보내세요! :dart:"
