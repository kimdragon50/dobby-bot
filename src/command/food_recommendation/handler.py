"""Food Recommendation Handler - /dobby food or /dobby bob

Recommends 3 menu items based on location, mood, weather, and preferences.
Uses LLM for recommendations and Naver Blog Search for restaurant links.
"""
import asyncio
import json
import threading
from typing import Any, Dict, Optional, List

from slack_bolt import App

from src._shared.logger import logger
from src._shared.llm import LLMService
from src.command.food_recommendation.prompts import SYSTEM_PROMPT, HUMAN_PROMPT
from src.command.food_recommendation.services import WeatherService, NaverSearchService


MOOD_OPTIONS = [
    {"label": "혼밥", "value": "혼밥"},
    {"label": "점심 간단히", "value": "점심 간단히"},
    {"label": "든든한 한 끼", "value": "든든한 한 끼"},
    {"label": "회식/단체", "value": "회식/단체"},
    {"label": "데이트/특별한 날", "value": "데이트/특별한 날"},
    {"label": "야근/야식", "value": "야근/야식"},
    {"label": "건강식", "value": "건강식"},
    {"label": "해장", "value": "해장"},
]

FOOD_TYPE_OPTIONS = [
    {"label": "한식", "value": "한식"},
    {"label": "중식", "value": "중식"},
    {"label": "일식", "value": "일식"},
    {"label": "양식", "value": "양식"},
    {"label": "동남아", "value": "동남아"},
    {"label": "분식", "value": "분식"},
    {"label": "패스트푸드", "value": "패스트푸드"},
    {"label": "아무거나", "value": "아무거나"},
]

PEOPLE_COUNT_OPTIONS = [
    {"label": "1명", "value": "1명"},
    {"label": "2-3명", "value": "2-3명"},
    {"label": "4-6명", "value": "4-6명"},
    {"label": "7명+", "value": "7명 이상"},
]

BUDGET_OPTIONS = [
    {"label": "만원 이하", "value": "만원 이하"},
    {"label": "1-2만원", "value": "1-2만원"},
    {"label": "2-3만원", "value": "2-3만원"},
    {"label": "3만원+", "value": "3만원 이상"},
    {"label": "상관없음", "value": "상관없음"},
]


class FoodRecommendationHandler:
    """Handler for food recommendation via Slack modal"""

    def __init__(self, app: App, llm_service: LLMService):
        self.app = app
        self.llm = llm_service

    def register_handlers(self):
        """Register Slack modal event handlers"""
        self.app.view("food_recommendation_modal")(self.handle_modal_submission)
        self.app.options("location_search")(self.handle_location_search)
        logger.info("FoodRecommendationHandler: registered modal handler")

    def handle_location_search(self, ack, options):
        """Handle external_select suggestion for location search"""
        query = options.get("value", "").strip()
        if not query:
            ack(options=[])
            return

        results = NaverSearchService.search_locations(query)
        slack_options = [
            {
                "text": {"type": "plain_text", "text": f"{r['title']} ({r['area']})"},
                "value": r["area"],
            }
            for r in results[:10]
        ]
        ack(options=slack_options)

    def handle_subcommand(self, command: dict, client):
        """Open food recommendation modal - called from /dobby food or /dobby bob"""
        try:
            channel_id = command["channel_id"]
            modal_view = self._build_modal_view(channel_id)
            client.views_open(trigger_id=command["trigger_id"], view=modal_view)
            logger.info("[Food] Opened food recommendation modal")
        except Exception as e:
            logger.error(f"[Food] Failed to open modal: {e}")
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=command["user_id"],
                text=f":x: 모달을 여는 중 오류가 발생했습니다: {e}",
            )

    def _build_modal_view(self, channel_id: str = "") -> Dict[str, Any]:
        """Build the modal view for food recommendation"""
        mood_options = [
            {
                "text": {"type": "plain_text", "text": opt["label"]},
                "value": opt["value"],
            }
            for opt in MOOD_OPTIONS
        ]

        food_type_options = [
            {
                "text": {"type": "plain_text", "text": opt["label"]},
                "value": opt["value"],
            }
            for opt in FOOD_TYPE_OPTIONS
        ]

        people_count_options = [
            {
                "text": {"type": "plain_text", "text": opt["label"]},
                "value": opt["value"],
            }
            for opt in PEOPLE_COUNT_OPTIONS
        ]

        budget_options = [
            {
                "text": {"type": "plain_text", "text": opt["label"]},
                "value": opt["value"],
            }
            for opt in BUDGET_OPTIONS
        ]

        return {
            "type": "modal",
            "callback_id": "food_recommendation_modal",
            "private_metadata": json.dumps({"channel_id": channel_id}),
            "title": {"type": "plain_text", "text": "오늘 뭐 먹지?"},
            "submit": {"type": "plain_text", "text": "추천받기"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "location_block",
                    "label": {"type": "plain_text", "text": "위치"},
                    "element": {
                        "type": "external_select",
                        "action_id": "location_search",
                        "placeholder": {"type": "plain_text", "text": "장소를 검색하세요 (예: 역삼, 판교)"},
                        "min_query_length": 1,
                    },
                },
                {
                    "type": "input",
                    "block_id": "mood_block",
                    "label": {"type": "plain_text", "text": "분위기"},
                    "element": {
                        "type": "static_select",
                        "action_id": "mood_select",
                        "placeholder": {"type": "plain_text", "text": "어떤 분위기로?"},
                        "options": mood_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "food_type_block",
                    "label": {"type": "plain_text", "text": "음식 종류"},
                    "optional": True,
                    "element": {
                        "type": "multi_static_select",
                        "action_id": "food_type_select",
                        "placeholder": {"type": "plain_text", "text": "선호하는 음식 종류 (선택 안 하면 아무거나)"},
                        "options": food_type_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "people_count_block",
                    "label": {"type": "plain_text", "text": "인원수"},
                    "optional": True,
                    "element": {
                        "type": "static_select",
                        "action_id": "people_count_select",
                        "placeholder": {"type": "plain_text", "text": "몇 명이서?"},
                        "options": people_count_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "budget_block",
                    "label": {"type": "plain_text", "text": "예산 (1인)"},
                    "optional": True,
                    "element": {
                        "type": "static_select",
                        "action_id": "budget_select",
                        "placeholder": {"type": "plain_text", "text": "1인 예산은?"},
                        "options": budget_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "restrictions_block",
                    "label": {"type": "plain_text", "text": "기피 음식"},
                    "optional": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "restrictions",
                        "placeholder": {"type": "plain_text", "text": "예: 해산물, 매운 음식, 회 등"},
                    },
                },
            ],
        }

    def handle_modal_submission(self, ack, view, client, body):
        """Handle modal submission"""
        ack()

        user_id = body["user"]["id"]
        values = view["state"]["values"]

        # Extract channel_id from private_metadata
        metadata = json.loads(view.get("private_metadata", "{}"))
        channel_id = metadata.get("channel_id", user_id)

        # Parse form values
        location_option = (
            values["location_block"]["location_search"]["selected_option"]
        )
        location_display = location_option["value"] if location_option else "강남"

        mood = values["mood_block"]["mood_select"]["selected_option"]["value"]

        food_type_options = (
            values.get("food_type_block", {})
            .get("food_type_select", {})
            .get("selected_options")
        )
        food_types = (
            [opt["value"] for opt in food_type_options]
            if food_type_options
            else ["아무거나"]
        )

        people_count_option = (
            values.get("people_count_block", {})
            .get("people_count_select", {})
            .get("selected_option")
        )
        people_count = people_count_option["value"] if people_count_option else "상관없음"

        budget_option = (
            values.get("budget_block", {})
            .get("budget_select", {})
            .get("selected_option")
        )
        budget = budget_option["value"] if budget_option else "상관없음"

        restrictions = (
            values.get("restrictions_block", {})
            .get("restrictions", {})
            .get("value")
            or "없음"
        )

        logger.info(
            f"[Food] Submission from {user_id}: "
            f"location={location_display}, mood={mood}, "
            f"food={food_types}, people={people_count}, budget={budget}"
        )

        # Send loading message to channel
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":cooking: AI가 오늘 날씨에 맞는 메뉴를 고르고 있습니다...",
            )
        except Exception as e:
            logger.error(f"[Food] Failed to send loading message: {e}")

        # Process in background thread
        thread = threading.Thread(
            target=self._process_recommendation,
            args=(
                user_id,
                channel_id,
                client,
                location_display,
                mood,
                food_types,
                people_count,
                budget,
                restrictions,
            ),
        )
        thread.start()

    def _process_recommendation(
        self,
        user_id: str,
        channel_id: str,
        client,
        location_display: str,
        mood: str,
        food_types: List[str],
        people_count: str,
        budget: str,
        restrictions: str,
    ):
        """Process food recommendation in background thread"""
        try:
            # 1. Get weather (use location name for wttr.in)
            weather_info = WeatherService.get_weather_by_name(location_display)
            weather_desc = weather_info["description"] if weather_info else "정보 없음"

            # 2. Call LLM for recommendations
            from datetime import datetime
            weekdays = ["월", "화", "수", "목", "금", "토", "일"]
            now = datetime.now()
            today_str = f"{now.strftime('%Y-%m-%d')} ({weekdays[now.weekday()]})"

            input_data = {
                "location": location_display,
                "today": today_str,
                "weather": weather_desc,
                "mood": mood,
                "food_type": ", ".join(food_types),
                "people_count": people_count,
                "budget": budget,
                "restrictions": restrictions,
            }

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self.llm.invoke_for_json(
                        SYSTEM_PROMPT, HUMAN_PROMPT, input_data, temperature=0.9
                    )
                )
            finally:
                loop.close()

            recommendations = result.get("recommendations", [])
            if not recommendations:
                self._send_fallback(client, channel_id)
                return

            # 3. 각 메뉴별 네이버 지역 검색으로 실제 식당 가져오기
            for rec in recommendations[:3]:
                keyword = rec.get("search_keyword", rec.get("menu", ""))
                rec["restaurants"] = NaverSearchService.search_restaurants_by_menu(
                    keyword, location_display
                )

            # 4. Post header message to channel
            header_text = self._format_header(
                location_display, weather_info, mood
            )
            header_result = client.chat_postMessage(
                channel=channel_id,
                text=header_text,
                unfurl_links=False,
                unfurl_media=False,
            )
            thread_ts = header_result["ts"]

            # 5. Post restaurant details as thread replies
            for i, rec in enumerate(recommendations[:3]):
                thread_text = self._format_restaurant_thread(i, rec)
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=thread_text,
                    unfurl_links=False,
                    unfurl_media=False,
                )

            logger.info(f"[Food] Sent recommendation to channel {channel_id}")

        except Exception as e:
            logger.error(f"[Food] Recommendation failed: {e}", exc_info=True)
            self._send_fallback(client, channel_id)

    def _get_weather_emoji(self, condition: str) -> str:
        """Pick weather emoji based on condition"""
        condition_lower = condition.lower()
        if any(w in condition_lower for w in ["비", "rain", "소나기", "shower"]):
            return ":rain_cloud:"
        elif any(w in condition_lower for w in ["눈", "snow"]):
            return ":snow_cloud:"
        elif any(w in condition_lower for w in ["흐림", "cloud", "overcast"]):
            return ":cloud:"
        elif any(w in condition_lower for w in ["안개", "fog", "mist"]):
            return ":fog:"
        return ":sunny:"

    def _format_header(
        self,
        location: str,
        weather_info: Optional[Dict],
        mood: str,
    ) -> str:
        """Format the header message posted to channel"""
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f":fork_and_knife: *오늘의 추천 메뉴!* ({location})  -  {today}"]

        if weather_info:
            condition = weather_info.get("condition", "")
            temp = weather_info.get("temp_c", "")
            weather_emoji = self._get_weather_emoji(condition)
            lines.append(
                f"{weather_emoji} 오늘 날씨: {condition}, {temp}°C  |  분위기: {mood}"
            )
        else:
            lines.append(f"분위기: {mood}")

        lines.append("")
        lines.append(":thread: 맛집 추천 리스트는 스레드를 확인하세요!")

        return "\n".join(lines)

    def _format_restaurant_thread(self, index: int, rec: Dict) -> str:
        """Format a single restaurant thread reply"""
        number_emojis = ["1\ufe0f\u20e3", "2\ufe0f\u20e3", "3\ufe0f\u20e3"]
        num_emoji = number_emojis[index] if index < len(number_emojis) else f"{index+1}."
        menu = rec.get("menu", "추천 메뉴")
        emoji = rec.get("emoji", ":fork_and_knife:")
        restaurants = rec.get("restaurants", [])

        lines = [f"{num_emoji} *{menu}* {emoji} 맛집 추천", ""]

        for rest in restaurants:
            name = rest.get("name", "")
            address = rest.get("address", "")
            distance_m = rest.get("distance_m")
            map_url = rest.get("map_url", "")

            # 도보 시간 계산 (평균 보행속도 분당 80m)
            if distance_m and distance_m < 2400:  # 30분(2.4km) 이내
                walk_min = max(1, round(distance_m / 80))
                dist_str = f":walking: 도보 {walk_min}분({distance_m}m)"
            elif distance_m:
                dist_str = f":walking: {distance_m}m"
            else:
                dist_str = ""

            lines.append(f":round_pushpin: *{name}*")
            info_parts = []
            if address:
                info_parts.append(f":house: {address}")
            if dist_str:
                info_parts.append(dist_str)
            if info_parts:
                lines.append(">  " + "  |  ".join(info_parts))
            if map_url:
                lines.append(f">  <{map_url}|[네이버지도]>")
            lines.append("")

        return "\n".join(lines)

    def _send_fallback(self, client, channel_id: str):
        """Send fallback message when LLM fails"""
        try:
            client.chat_postMessage(
                channel=channel_id,
                text=(
                    ":warning: 메뉴 추천 중 오류가 발생했습니다.\n"
                    "다시 시도해주세요! (`/dobby food`)"
                ),
            )
        except Exception as e:
            logger.error(f"[Food] Failed to send fallback message: {e}")
