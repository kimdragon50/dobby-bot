"""Google Calendar API and briefing generation services"""
import json
import os
import zoneinfo
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src._shared.config import Config
from src._shared.logger import logger

KST = zoneinfo.ZoneInfo("Asia/Seoul")
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


@dataclass
class CalendarEvent:
    title: str
    start_time: str  # "09:00" or "종일"
    end_time: str  # "10:00" or "종일"
    location: Optional[str] = None
    attendees: List[str] = field(default_factory=list)
    meeting_link: Optional[str] = None


class CalendarService:
    """Google Calendar API service"""

    @classmethod
    def get_service_account_email(cls) -> Optional[str]:
        """서비스 계정 이메일 반환 (캘린더 공유 안내용)"""
        sa_file = Config.GOOGLE_SERVICE_ACCOUNT_FILE
        if not sa_file or not os.path.exists(sa_file):
            return None
        try:
            with open(sa_file, "r") as f:
                data = json.load(f)
                return data.get("client_email")
        except Exception:
            return None

    @classmethod
    def fetch_events(
        cls, calendar_id: str, target_date: Optional[date] = None
    ) -> List[CalendarEvent]:
        """Google Calendar에서 특정 날짜의 일정 조회"""
        if target_date is None:
            target_date = datetime.now(KST).date()

        sa_file = Config.GOOGLE_SERVICE_ACCOUNT_FILE
        if not sa_file or not os.path.exists(sa_file):
            raise RuntimeError(
                f"Service account file not found: {sa_file}"
            )

        creds = Credentials.from_service_account_file(sa_file, scopes=SCOPES)
        service = build("calendar", "v3", credentials=creds)

        time_min = datetime.combine(target_date, time.min, tzinfo=KST).isoformat()
        time_max = datetime.combine(
            target_date + timedelta(days=1), time.min, tzinfo=KST
        ).isoformat()

        logger.info(
            f"[Calendar] Fetching events for {target_date} (calendar: {calendar_id})"
        )

        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events: List[CalendarEvent] = []
        for item in result.get("items", []):
            if _is_declined(item, calendar_id):
                continue

            start = item.get("start", {})
            end = item.get("end", {})

            if "dateTime" in start:
                start_str = _parse_time(start["dateTime"])
                end_str = _parse_time(end.get("dateTime", ""))
            else:
                start_str = "종일"
                end_str = "종일"

            attendees = [
                a["email"]
                for a in item.get("attendees", [])
                if a.get("responseStatus") != "declined"
            ]

            meeting_link = item.get("hangoutLink")
            if not meeting_link:
                conf_data = item.get("conferenceData", {})
                for entry in conf_data.get("entryPoints", []):
                    if entry.get("entryPointType") == "video":
                        meeting_link = entry.get("uri")
                        break

            events.append(
                CalendarEvent(
                    title=item.get("summary", "(제목 없음)"),
                    start_time=start_str,
                    end_time=end_str,
                    location=item.get("location"),
                    attendees=attendees,
                    meeting_link=meeting_link,
                )
            )

        logger.info(f"[Calendar] Found {len(events)} events for {target_date}")
        return events


class BriefingService:
    """브리핑 텍스트 포맷팅 서비스"""

    @staticmethod
    def format_events_for_prompt(events: List[CalendarEvent]) -> str:
        """이벤트 목록을 LLM 프롬프트용 텍스트로 변환"""
        parts = []
        for i, event in enumerate(events, 1):
            parts.append(f"\n### 일정 {i}")
            parts.append(f"- 제목: {event.title}")
            parts.append(f"- 시간: {event.start_time} ~ {event.end_time}")
            if event.location:
                parts.append(f"- 장소: {event.location}")
            if event.attendees:
                parts.append(f"- 참석자: {', '.join(event.attendees)}")
            if event.meeting_link:
                parts.append(f"- 회의 링크: {event.meeting_link}")
        return "\n".join(parts)

    @staticmethod
    def format_date_str(target_date: date) -> str:
        """날짜를 한국어 포맷으로 변환"""
        weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
        weekday = weekday_names[target_date.weekday()]
        return f"{target_date.strftime('%Y-%m-%d')} ({weekday})"


def _is_declined(event: dict, calendar_id: str) -> bool:
    """Check if the calendar owner declined this event."""
    for attendee in event.get("attendees", []):
        if attendee.get("email", "").lower() == calendar_id.lower():
            return attendee.get("responseStatus") == "declined"
        if attendee.get("self", False):
            return attendee.get("responseStatus") == "declined"
    return False


def _parse_time(dt_string: str) -> str:
    """Parse RFC3339 datetime string to 'HH:MM' format in KST."""
    if not dt_string:
        return ""
    try:
        dt = datetime.fromisoformat(dt_string)
        dt_kst = dt.astimezone(KST)
        return dt_kst.strftime("%H:%M")
    except (ValueError, TypeError):
        return dt_string
