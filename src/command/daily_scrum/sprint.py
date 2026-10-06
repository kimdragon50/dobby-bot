"""가장 최근 스프린트 추리기

스크럼에서 보고 싶은 건 "이번 스프린트에 뭘 들고 있나" 다. JQL 전체를 뿌리면
지난 분기 완료 티켓까지 섞여 읽히지 않는다.

"가장 최근" 의 정의
  1. 진행 중(active)인 스프린트가 있으면 그것. 여러 개면 늦게 시작한 쪽.
  2. 없으면 가장 최근에 끝난(closed) 스프린트.

2번이 필요한 이유 — 스프린트 사이에는 공백이 생긴다. 몇 주씩 활성
스프린트가 없는 기간도 있는데, 그 기간에 `openSprints()` 만 쓰면 매일 "티켓 없음" 만 올라간다. 직전
스프린트를 보여주는 편이 스크럼에 쓸모 있다.

미래(future) 스프린트는 고르지 않는다. 아직 시작도 안 한 계획을 "현재
현황" 으로 내보내면 오해를 부른다.
"""
from typing import Dict, List, Optional

# 정렬용 하한 — 날짜가 비어 있는 스프린트를 맨 뒤로 보낸다
_NO_DATE = ""


def _active(s: Dict) -> bool:
    return s.get("state") == "active"


def _closed(s: Dict) -> bool:
    return s.get("state") == "closed"


def latest_sprint(issues: List[Dict]) -> Optional[Dict]:
    """이슈들에 붙은 스프린트 중 '가장 최근' 하나를 고른다.

    보드 ID 를 받지 않는다 — 운영자가 JQL 을 바꿔도 그대로 동작해야 하므로
    조회 결과에 실제로 들어 있는 스프린트만 후보로 삼는다.
    """
    by_id: Dict[int, Dict] = {}
    for it in issues:
        for s in it.get("sprints") or []:
            by_id.setdefault(s["id"], s)
    if not by_id:
        return None

    pool = list(by_id.values())
    active = [s for s in pool if _active(s)]
    if active:
        # 늦게 시작한 쪽이 현재 스프린트
        return max(active, key=lambda s: (s.get("start") or _NO_DATE, s["id"]))

    closed = [s for s in pool if _closed(s)]
    if closed:
        return max(closed, key=lambda s: (s.get("end") or _NO_DATE, s["id"]))

    return None


def filter_by_sprint(issues: List[Dict], sprint: Optional[Dict]) -> List[Dict]:
    """해당 스프린트에 속한 이슈만 남긴다.

    이월 이슈는 스프린트를 여러 개 갖는다 — 그중 하나라도 일치하면 포함이다.
    sprint 가 None 이면(스프린트를 안 쓰는 프로젝트) 전량을 그대로 돌려준다.
    """
    if not sprint:
        return issues
    sid = sprint["id"]
    return [i for i in issues if any(s["id"] == sid for s in (i.get("sprints") or []))]


def sprint_label(sprint: Optional[Dict]) -> str:
    """헤더에 쓸 '어느 스프린트를 보고 있는지' 표기.

    닫힌 스프린트로 폴백한 경우를 드러낸다 — 안 그러면 지난 스프린트를
    이번 것으로 착각한다.
    """
    if not sprint:
        return ""
    name = sprint.get("name") or f"#{sprint['id']}"
    period = ""
    if sprint.get("start") and sprint.get("end"):
        period = f" ({sprint['start']} ~ {sprint['end']})"
    if _active(sprint):
        return f"{name}{period}"
    return f"{name}{period} · _종료된 스프린트_"
