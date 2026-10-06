"""External services for food recommendation

- WeatherService: wttr.in 사용 (무료, API key 불필요)
- NaverSearchService: 네이버 검색 API로 실제 블로그/지도 URL 조회
"""
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from src._shared.config import Config
from src._shared.logger import logger


# ============================================
# Location → City mapping for wttr.in
# ============================================

LOCATION_CITY_MAP = {
    "gangnam": "Gangnam,Seoul",
    "pangyo": "Bundang,Seongnam",
    "seocho": "Seocho,Seoul",
    "jamsil": "Songpa,Seoul",
    "yeouido": "Yeongdeungpo,Seoul",
    "hongdae": "Mapo,Seoul",
    "jongno": "Jongno,Seoul",
    "guro": "Guro,Seoul",
    "seongsu": "Seongdong,Seoul",
}


class WeatherService:
    """wttr.in을 사용한 날씨 조회 서비스 (API key 불필요)"""

    # 날씨 condition 한글 매핑 (wttr.in weatherDesc 기준)
    CONDITION_MAP = {
        "sunny": "맑음",
        "clear": "맑음",
        "partly cloudy": "구름 조금",
        "cloudy": "흐림",
        "overcast": "흐림",
        "mist": "안개",
        "fog": "안개",
        "patchy rain possible": "비 가능성",
        "patchy rain nearby": "비 가능성",
        "light rain": "가벼운 비",
        "light rain shower": "가벼운 소나기",
        "moderate rain": "비",
        "moderate rain at times": "비",
        "heavy rain": "폭우",
        "moderate or heavy rain shower": "소나기",
        "patchy snow possible": "눈 가능성",
        "light snow": "가벼운 눈",
        "moderate snow": "눈",
        "heavy snow": "폭설",
        "thundery outbreaks possible": "천둥 가능성",
        "patchy light rain with thunder": "천둥 동반 비",
    }

    @classmethod
    def get_weather_by_name(cls, location_name: str) -> Optional[Dict]:
        """위치 이름으로 날씨 조회 (네이버 검색 기반 위치용)

        Args:
            location_name: 장소명 (예: "역삼동 맛집", "강남역")

        Returns:
            {"temp_c": 15, "condition": "맑음", "description": "맑음, 15°C"} or None
        """
        # 장소명에서 도시 추출 시도, 기본값 Seoul
        city = "Seoul"
        for key, value in LOCATION_CITY_MAP.items():
            # key 또는 한글 지역명이 포함되어 있으면 매칭
            if key in location_name.lower():
                city = value
                break
        # 한글 지역명 매핑
        kr_map = {
            "강남": "Gangnam,Seoul", "역삼": "Gangnam,Seoul",
            "판교": "Bundang,Seongnam", "분당": "Bundang,Seongnam",
            "서초": "Seocho,Seoul", "양재": "Seocho,Seoul",
            "잠실": "Songpa,Seoul", "송파": "Songpa,Seoul",
            "여의도": "Yeongdeungpo,Seoul",
            "홍대": "Mapo,Seoul", "합정": "Mapo,Seoul",
            "종로": "Jongno,Seoul", "광화문": "Jongno,Seoul",
            "구로": "Guro,Seoul", "가산": "Guro,Seoul",
            "성수": "Seongdong,Seoul", "건대": "Seongdong,Seoul",
        }
        for kr_name, kr_city in kr_map.items():
            if kr_name in location_name:
                city = kr_city
                break

        return cls._fetch_weather(city)

    @classmethod
    def get_weather(cls, location_key: str) -> Optional[Dict]:
        """wttr.in으로 현재 날씨 조회 (API key 불필요)

        Args:
            location_key: 위치 키 (예: "gangnam", "pangyo")

        Returns:
            {"temp_c": 15, "condition": "맑음", "description": "맑음, 15°C"} or None
        """
        city = LOCATION_CITY_MAP.get(location_key, "Seoul")
        return cls._fetch_weather(city)

    @classmethod
    def _fetch_weather(cls, city: str) -> Optional[Dict]:
        """wttr.in에서 날씨 데이터 조회"""

        try:
            # wttr.in JSON format (API key 불필요)
            url = f"https://wttr.in/{quote(city)}?format=j1"
            response = requests.get(
                url,
                headers={"Accept": "application/json"},
                timeout=Config.get_http_timeout(),
            )
            response.raise_for_status()
            data = response.json()

            current = data.get("current_condition", [{}])[0]
            temp_c = int(current.get("temp_C", 0))

            # 한글 변환: lang_ko 필드 우선, 없으면 영어 → 매핑
            lang_ko = current.get("lang_ko", [{}])
            if lang_ko and lang_ko[0].get("value"):
                condition_kr = lang_ko[0]["value"]
            else:
                condition_en = current.get("weatherDesc", [{}])[0].get("value", "").lower()
                condition_kr = cls.CONDITION_MAP.get(condition_en, condition_en)

            result = {
                "temp_c": temp_c,
                "condition": condition_kr,
                "description": f"{condition_kr}, {temp_c}°C",
            }
            logger.info(f"[Weather] {city}: {result['description']}")
            return result

        except Exception as e:
            logger.error(f"[Weather] Failed to get weather for {city}: {e}")
            return None


class NaverSearchService:
    """네이버 검색 API를 사용한 실제 블로그/지도 URL 조회 서비스"""

    BLOG_API_URL = "https://openapi.naver.com/v1/search/blog.json"
    LOCAL_API_URL = "https://openapi.naver.com/v1/search/local.json"

    @classmethod
    def _get_headers(cls) -> Optional[Dict]:
        """네이버 API 인증 헤더 반환. 키가 없으면 None."""
        client_id = Config.NAVER_CLIENT_ID
        client_secret = Config.NAVER_CLIENT_SECRET
        if not client_id or not client_secret:
            return None
        return {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }

    @staticmethod
    def _extract_area(address: str) -> str:
        """주소에서 구/동 단위 지역명 추출

        Args:
            address: 도로명 또는 지번 주소 (예: "서울특별시 강남구 역삼동 123-4")

        Returns:
            "강남구 역삼동" 또는 원본 주소
        """
        import re
        # "시/도 구/군 동/읍/면" 패턴 추출
        m = re.search(r"(\S+[구군])\s+(\S+[동읍면가로길])", address)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        # 구만이라도 추출
        m = re.search(r"(\S+[구군])", address)
        if m:
            return m.group(1)
        return address

    @classmethod
    def search_locations(cls, query: str) -> List[Dict]:
        """네이버 지역 검색 API로 장소 후보 목록 반환 (external_select용)

        Args:
            query: 사용자 입력 (예: "역삼", "강남역")

        Returns:
            [{"title": "장소명", "address": "주소", "area": "구/동 지역명"}]
        """
        headers = cls._get_headers()
        if not headers or not query or len(query) < 1:
            return []

        try:
            response = requests.get(
                cls.LOCAL_API_URL,
                headers=headers,
                params={"query": query, "display": 10},
                timeout=Config.get_http_timeout(),
            )
            response.raise_for_status()
            items = response.json().get("items", [])

            results = []
            seen = set()
            for item in items:
                # HTML 태그 제거
                title = item.get("title", "").replace("<b>", "").replace("</b>", "")
                address = item.get("roadAddress", "") or item.get("address", "")
                area = cls._extract_area(address)
                if title and area not in seen:
                    seen.add(area)
                    results.append({
                        "title": title,
                        "address": address,
                        "area": area,
                    })
            return results

        except Exception as e:
            logger.error(f"[NaverSearch] Location search failed for '{query}': {e}")
            return []

    @staticmethod
    def _katec_to_wgs84(mapx: str, mapy: str) -> tuple:
        """네이버 지역 검색 좌표(KATEC)를 WGS84 경위도로 근사 변환

        KATEC: 중심경도 128°E, 원점위도 38°N,
               False Easting 400,000m, False Northing 600,000m
        """
        try:
            x = int(mapx)
            y = int(mapy)
            lng = 128.0 + (x - 400000) / 88300
            lat = 38.0 + (y - 600000) / 111320
            return lat, lng
        except (ValueError, TypeError):
            return None, None

    @staticmethod
    def _haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """두 좌표 사이 거리(미터) 계산"""
        import math
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lng2 - lng1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @classmethod
    def _get_location_coords(cls, location: str) -> tuple:
        """지역명으로 기준 좌표 조회 (네이버 지역 검색 첫 결과 사용)"""
        headers = cls._get_headers()
        if not headers:
            return None, None
        try:
            response = requests.get(
                cls.LOCAL_API_URL,
                headers=headers,
                params={"query": location, "display": 1},
                timeout=Config.get_http_timeout(),
            )
            response.raise_for_status()
            items = response.json().get("items", [])
            if items:
                return cls._katec_to_wgs84(items[0].get("mapx", ""), items[0].get("mapy", ""))
        except Exception as e:
            logger.error(f"[NaverSearch] Location coord lookup failed: {e}")
        return None, None

    @classmethod
    def _search_local(cls, query: str, headers: Dict,
                      base_lat: float = None, base_lng: float = None,
                      count: int = 3) -> List[Dict]:
        """네이버 지역 검색으로 식당 목록 반환 (거리 계산 포함)"""
        try:
            response = requests.get(
                cls.LOCAL_API_URL,
                headers=headers,
                params={"query": query, "display": 10},
                timeout=Config.get_http_timeout(),
            )
            response.raise_for_status()
            items = response.json().get("items", [])
        except Exception as e:
            logger.error(f"[NaverSearch] Local search failed for '{query}': {e}")
            return []

        # 음식점 카테고리 필터링 (없으면 전체 사용)
        food_kw = ["음식", "식당", "카페", "한식", "중식", "일식", "양식",
                    "분식", "패스트푸드", "뷔페", "치킨", "피자"]
        food_items = [item for item in items
                      if any(kw in item.get("category", "") for kw in food_kw)]
        target_items = food_items if food_items else items

        results = []
        seen_names = set()
        for item in target_items:
            if len(results) >= count:
                break
            name = item.get("title", "").replace("<b>", "").replace("</b>", "")
            if name in seen_names:
                continue
            seen_names.add(name)

            address = item.get("roadAddress", "") or item.get("address", "")
            map_url = f"https://map.naver.com/v5/search/{quote(name)}"

            # 거리 계산
            dist = None
            if base_lat and base_lng:
                item_lat, item_lng = cls._katec_to_wgs84(
                    item.get("mapx", ""), item.get("mapy", "")
                )
                if item_lat and item_lng:
                    dist = cls._haversine_distance(base_lat, base_lng, item_lat, item_lng)
                    logger.debug(
                        f"[NaverSearch] {name}: base({base_lat:.4f},{base_lng:.4f}) "
                        f"item({item_lat:.4f},{item_lng:.4f}) dist={dist:.0f}m "
                        f"mapx={item.get('mapx')} mapy={item.get('mapy')}"
                    )

            results.append({
                "name": name,
                "address": address,
                "distance_m": round(dist) if dist else None,
                "map_url": map_url,
            })

        return results

    @classmethod
    def search_restaurants_by_menu(cls, menu_keyword: str, location: str, count: int = 3) -> List[Dict]:
        """메뉴 키워드로 해당 지역의 실제 식당을 검색

        네이버 지역 검색은 쿼리에 포함된 지역명으로 자동 근접 정렬됩니다.

        Returns:
            [{"name": "상호명", "address": "주소", "map_url": "지도 URL"}]
        """
        headers = cls._get_headers()
        if not headers:
            return [cls._fallback_restaurant(menu_keyword, location)]

        base_lat, base_lng = cls._get_location_coords(location)
        if base_lat and base_lng:
            logger.info(f"[NaverSearch] Base coords for '{location}': lat={base_lat:.4f}, lng={base_lng:.4f}")

        # 1차: "지역 메뉴" 검색
        restaurants = cls._search_local(
            f"{location} {menu_keyword}", headers, base_lat, base_lng, count
        )

        # 2차: 결과 부족 시 "지역 메뉴 맛집" 으로 재검색
        if len(restaurants) < count:
            extra = cls._search_local(
                f"{location} {menu_keyword} 맛집", headers, base_lat, base_lng, count
            )
            seen = {r["name"] for r in restaurants}
            for r in extra:
                if len(restaurants) >= count:
                    break
                if r["name"] not in seen:
                    restaurants.append(r)

        logger.info(
            f"[NaverSearch] Found {len(restaurants)} restaurants "
            f"for '{location} {menu_keyword}'"
        )

        if not restaurants:
            restaurants.append(cls._fallback_restaurant(menu_keyword, location))

        return restaurants

    @staticmethod
    def _fallback_restaurant(menu_keyword: str, location: str) -> Dict:
        """API 실패 시 검색 URL fallback"""
        keyword = f"{location} {menu_keyword}"
        return {
            "name": f"{location} {menu_keyword} 맛집",
            "map_url": f"https://map.naver.com/v5/search/{quote(keyword)}",
        }
