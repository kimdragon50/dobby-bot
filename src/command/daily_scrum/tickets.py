"""데일리 스크럼 티켓 현황 — JQL 조회 결과를 담당자별로 정리

스크럼 스레드 맨 앞에 "누가 어떤 티켓을 들고 있는지" 를 붙여, 각자 스크럼을
쓸 때 참고하도록 한다.

설계 메모
  - 진행 중 티켓을 먼저 보여준다. 스크럼에서 알고 싶은 건 "오늘 뭐 하나" 이고,
    완료 티켓은 참고 정보라 뒤로 보낸다.
  - 미할당 티켓은 숨기지 않는다. 스크럼에서 주인을 정해야 할 항목이다.
  - Slack 블록은 50개 제한이 있어 담당자가 많으면 한 섹션에 묶는다.
"""
from typing import Dict, List

# Jira statusCategory key — 완료 판정에 쓴다 (상태 이름은 프로젝트마다 달라 신뢰 못 함)
_DONE_CATEGORY = "done"
_UNASSIGNED = "미할당"

# 한 담당자당 표시할 최대 티켓 수 (너무 길면 스레드가 읽기 어려워진다)
_MAX_PER_PERSON = 12

# Slack 제한 — 섹션 text 는 3000자, 메시지당 블록은 50개.
# 넘기면 invalid_blocks 로 게시 자체가 실패한다 (2026-10-06 실제 발생).
_SECTION_LIMIT = 2900   # 3000 에 여유를 둔다
_MAX_BLOCKS = 45        # divider/헤더/컨텍스트 몫을 남긴다


def _pack_sections(parts: List[str], limit: int = _SECTION_LIMIT) -> List[str]:
    """담당자별 텍스트를 글자 수 한도 안에서 묶는다.

    사람 수로 묶으면(예: 3명씩) 티켓이 많은 날 한도를 넘어 게시가 실패한다.
    한 사람 텍스트가 혼자 한도를 넘으면 잘라서 넣는다.
    """
    sections: List[str] = []
    buf = ""
    for p in parts:
        if len(p) > limit:
            if buf:
                sections.append(buf)
                buf = ""
            sections.append(p[: limit - 20].rstrip() + "\n    • _…생략_")
            continue
        candidate = f"{buf}\n\n{p}" if buf else p
        if len(candidate) > limit:
            sections.append(buf)
            buf = p
        else:
            buf = candidate
    if buf:
        sections.append(buf)
    return sections


def _is_done(issue: Dict) -> bool:
    cat = (issue.get("status_category") or "").lower()
    if cat:
        return cat == _DONE_CATEGORY
    # statusCategory 를 못 받은 경우의 보수적 폴백
    return (issue.get("status") or "") in ("완료", "Done", "Closed")


def group_by_assignee(issues: List[Dict]) -> Dict[str, List[Dict]]:
    """담당자별로 묶는다. 미할당은 맨 뒤로 가도록 별도 키를 쓴다."""
    grouped: Dict[str, List[Dict]] = {}
    for it in issues:
        name = (it.get("assignee") or "").strip() or _UNASSIGNED
        grouped.setdefault(name, []).append(it)
    for v in grouped.values():
        # 진행 중 먼저, 그 안에서는 키 순서로 안정 정렬
        v.sort(key=lambda x: (_is_done(x), x.get("key", "")))
    return grouped


def _person_line(name: str, items: List[Dict]) -> str:
    open_items = [i for i in items if not _is_done(i)]
    done_items = [i for i in items if _is_done(i)]

    head = f"*{name}* · 진행 {len(open_items)} / 완료 {len(done_items)}"
    lines = [head]
    for it in open_items[:_MAX_PER_PERSON]:
        due = it.get("due_date") or ""
        due_txt = f" · 마감 {due}" if due else ""
        lines.append(
            f"    • <{it['url']}|{it['key']}> {it['summary']}  _{it['status']}_{due_txt}"
        )
    hidden = len(open_items) - _MAX_PER_PERSON
    if hidden > 0:
        lines.append(f"    • _…외 {hidden}건_")
    if not open_items:
        lines.append("    • _진행 중인 티켓 없음_")
    return "\n".join(lines)


def ticket_blocks(
    issues: List[Dict], jql_label: str = "", total: int = 0, sprint_label: str = ""
) -> List[Dict]:
    """티켓 현황 블록. 조회 결과가 없으면 그 사실을 알린다.

    total 은 JQL 전체 건수 — 표시 건수와 다르면 잘렸다는 사실을 드러낸다.
    sprint_label 은 어느 스프린트를 보고 있는지 (빈 값이면 스프린트 미사용).
    """
    if not issues:
        empty = "🎫 *오늘의 티켓 현황*\n조회된 티켓이 없습니다."
        if sprint_label:
            empty = f"🎫 *오늘의 티켓 현황* — {sprint_label}\n이 스프린트에 티켓이 없습니다."
        return [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": empty}},
        ]

    grouped = group_by_assignee(issues)
    # 미할당은 마지막, 나머지는 진행 중 많은 순 → 이름 순
    def sort_key(kv):
        name, items = kv
        open_cnt = sum(1 for i in items if not _is_done(i))
        return (name == _UNASSIGNED, -open_cnt, name)

    total_open = sum(1 for i in issues if not _is_done(i))
    shown = len(issues)
    grand = total if total and total > shown else shown
    scope = f" — {sprint_label}" if sprint_label else ""
    head = (
        f"🎫 *오늘의 티켓 현황*{scope}\n"
        f"전체 {grand}건 (진행 {total_open} / 완료 {shown - total_open})"
    )
    if grand > shown:
        head += f"\n_표시는 최근 {shown}건까지입니다._"
    blocks = [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
    ]

    parts = [_person_line(name, items) for name, items in sorted(grouped.items(), key=sort_key)]
    sections = _pack_sections(parts)

    room = _MAX_BLOCKS - len(blocks) - (1 if jql_label else 0)
    for s in sections[:room]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": s}})
    if len(sections) > room:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_표시 한도로 담당자 일부는 생략했습니다._"}
                ],
            }
        )

    if jql_label:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": jql_label}]}
        )
    return blocks
