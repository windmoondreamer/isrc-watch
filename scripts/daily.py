"""매일 아침 노션 루틴 — 결정론적 부분 전부.

클로드 클라우드 루틴이 하던 일 중 판단이 필요 없는 것들을 코드로 옮겼다.
LLM이 필요한 두 브리핑(반도체 소식·세계 경제)만 클로드 루틴에 남아 있다.

하는 일:
  1. 어제 🛏️ 체크박스 읽기 → 실행 캘린더 '메모'에 `취침: 성공/실패` 기록
  2. 당일 스케줄 리셋 — 날짜·세션 줄 갱신, 체크박스 전부 해제
  3. D-Day 갱신 — 자격증 표 + ⚡ 지금 당장 TOP 3
  4. 마감 임박 / 접수 시작 예정 콜아웃 재작성 (교육·강의 + 공모전 DB)
  5. 마감 지난 항목 상태를 '마감'으로, 접수 시작된 '예정'을 '신청중'으로
  6. 월요일 → 주간 회고 행 생성 / 토요일 → 신체 측정 행 생성

순서 주의: 1번(🛏️ 읽기)이 2번(체크박스 해제)보다 반드시 먼저다.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta

from notion import (  # noqa: E402
    BLK_CALLOUT_DEADLINE,
    BLK_CALLOUT_UPCOMING,
    BLK_CERT_TABLE,
    BLK_SESSION_LINE,
    BLK_SLEEP_TODO,
    BLK_TOP3,
    BODY_DB,
    CALENDAR_DB,
    CERT_ROWS,
    CONTEST_DB,
    EDU_DB,
    EXAMS,
    KST,
    MAIN_PAGE,
    RETRO_DB,
    SESSIONS,
    WEEKDAY_KR,
    block_text,
    prop_date,
    prop_select,
    prop_text,
    prop_title,
    query_all,
)
import notion as _n

_real_api = _n.api


def api(path, method="GET", data=None):
    """dry-run에서는 쓰기(POST/PATCH)를 실제로 보내지 않고 로그만 남긴다."""
    if DRY and method in ("POST", "PATCH") and "/query" not in path:
        print(f"  [DRY] {method} {path}")
        return {"id": "dry", "properties": {}, "table_row": {"cells": []}, "type": "to_do",
                "to_do": {"checked": False, "rich_text": []}}
    return _real_api(path, method, data)

log = []
DRY = "--dry-run" in sys.argv


def note(msg):
    log.append(msg)
    print(msg)


def rt(text):
    """긴 텍스트는 노션 rich_text 항목당 2000자 제한이 있어 잘라 담는다."""
    return [{"type": "text", "text": {"content": text[i:i + 1900]}} for i in range(0, len(text), 1900)] or [
        {"type": "text", "text": {"content": ""}}
    ]


def set_block_text(block_id, block_type, text):
    api(f"/blocks/{block_id}", "PATCH", {block_type: {"rich_text": rt(text)}})


def set_callout(block_id, heading, body):
    """콜아웃 = 볼드 제목 + 일반 본문.

    노션 API의 rich_text는 마크다운을 해석하지 않는다. '**볼드**'라고 쓰면
    별표가 그대로 보이므로 annotations로 굵게 만들어야 한다.
    """
    api(f"/blocks/{block_id}", "PATCH", {
        "quote": {
            "rich_text": [
                {"type": "text", "text": {"content": heading}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": body[:1900]}},
            ]
        }
    })


# ---------- 1. 수면 기록 ----------

def read_sleep_checkbox():
    b = api(f"/blocks/{BLK_SLEEP_TODO}")
    if b["type"] != "to_do" or "🛏️" not in block_text(b):
        note("⚠️ 🛏️ 체크박스 블록을 못 찾음 — 수면 기록 건너뜀")
        return None
    return b["to_do"]["checked"]


def record_sleep(yesterday, success):
    """실행 캘린더의 어제 행 '메모'에 취침 성공/실패 한 줄 덧붙인다."""
    if success is None:
        return
    rows = [p for p in query_all(CALENDAR_DB) if prop_date(p, "날짜") == yesterday]
    if not rows:
        note(f"⚠️ 실행 캘린더에 {yesterday} 행이 없어 수면 기록 못 함")
        return
    line = f"취침: {'성공' if success else '실패'}"
    page = rows[0]
    memo = prop_text(page, "메모")
    if line in memo:
        return
    new = f"{memo}\n{line}".strip()
    api(f"/pages/{page['id']}", "PATCH", {"properties": {"메모": {"rich_text": rt(new)}}})
    note(f"수면 기록: {yesterday} → {line}")


# ---------- 2. 당일 스케줄 리셋 ----------

def reset_schedule(today, weekday):
    session = SESSIONS[weekday]
    text = f"📆 **{today} ({WEEKDAY_KR[weekday]})** | 💪 세션: **{session}**"
    # 볼드가 살아 있어야 하므로 rich_text를 직접 구성한다.
    api(
        f"/blocks/{BLK_SESSION_LINE}",
        "PATCH",
        {
            "quote": {
                "rich_text": [
                    {"type": "text", "text": {"content": "📆 "}},
                    {"type": "text", "text": {"content": f"{today} ({WEEKDAY_KR[weekday]})"},
                     "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": " | 💪 세션: "}},
                    {"type": "text", "text": {"content": session}, "annotations": {"bold": True}},
                ]
            }
        },
    )
    note(f"스케줄 갱신: {today} ({WEEKDAY_KR[weekday]}) · {session}")

    # 📚 공부 / 🔁 루틴 섹션의 체크박스만 해제한다.
    # ⚡ 지금 당장 TOP 3, 💼 인턴 준비의 체크박스는 주간 항목이라 건드리지 않는다.
    children = api(f"/blocks/{MAIN_PAGE}/children?page_size=100")["results"]
    in_daily = False
    cleared = 0
    for b in children:
        t = block_text(b)
        if b["type"] == "paragraph" and ("📚 공부" in t or "🔁 루틴" in t):
            in_daily = True
            continue
        if b["type"] == "heading_2":  # 다음 섹션 진입 → 일일 체크박스 구간 종료
            in_daily = False
        if in_daily and b["type"] == "to_do" and b["to_do"]["checked"]:
            api(f"/blocks/{b['id']}", "PATCH", {"to_do": {"checked": False}})
            cleared += 1
    note(f"체크박스 해제: {cleared}개")


# ---------- 3. D-Day ----------

def dday(today, exam):
    return (datetime.strptime(exam, "%Y-%m-%d").date() - today).days


def update_dday(today):
    for cert, exam in EXAMS.items():
        d = dday(today, exam)
        status = "☑ 합격" if d < 0 else ("⚠️ 시험임박" if d <= 7 else "☐ 공부중")

        row = api(f"/blocks/{CERT_ROWS[cert]}")
        cells = row["table_row"]["cells"]
        cells[2] = [{"type": "text", "text": {"content": f"D-{max(d, 0)}"}}]
        cells[4] = [{"type": "text", "text": {"content": status}}]
        api(f"/blocks/{CERT_ROWS[cert]}", "PATCH", {"table_row": {"cells": cells}})

        # ⚡ 지금 당장 TOP 3의 "(D-25 · 8/8)" 표기도 맞춘다.
        if cert in BLK_TOP3:
            b = api(f"/blocks/{BLK_TOP3[cert]}")
            text = block_text(b)
            new = re.sub(r"D-\d+", f"D-{max(d, 0)}", text)
            if new != text:
                set_block_text(BLK_TOP3[cert], "to_do", new)
    note(f"D-Day 갱신: " + " · ".join(f"{c} D-{max(dday(today, e), 0)}" for c, e in EXAMS.items()))


# ---------- 4~5. 마감 콜아웃 + 상태 동기화 ----------

def parse_start_date(note_text, year):
    """'주요 일정' 텍스트에서 접수 시작일을 뽑는다. 'MM/DD' 또는 'YYYY-MM-DD' 지원."""
    m = re.search(r"(?:접수|신청)\s*(\d{4})-(\d{2})-(\d{2})", note_text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(?:접수|신청)\s*(\d{1,2})/(\d{1,2})", note_text)
    if m:
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def sync_and_callouts(today):
    items = []  # (마감일, 제목, 상태, 종류)
    upcoming = []

    for db_id, title_prop, date_prop, kind in (
        (EDU_DB, "교육명", "신청마감", "edu"),
        (CONTEST_DB, "대회명", "접수마감", "contest"),
    ):
        for page in query_all(db_id):
            status = prop_select(page, "상태")
            if status == "마감":
                continue
            title = prop_title(page, title_prop)
            deadline = prop_date(page, date_prop)
            memo = prop_text(page, "주요 일정")

            # 마감 지난 항목 → '마감'
            if deadline and deadline[:10] < today.isoformat():
                api(f"/pages/{page['id']}", "PATCH",
                    {"properties": {"상태": {"select": {"name": "마감"}}}})
                note(f"상태 '마감' 처리: {title[:40]}")
                continue

            # 접수 시작된 '예정' → '신청중'/'접수중'
            start = parse_start_date(memo, today.year)
            open_name = "신청중" if kind == "edu" else "접수중"
            if status == "예정" and start and start <= today.isoformat():
                api(f"/pages/{page['id']}", "PATCH",
                    {"properties": {"상태": {"select": {"name": open_name}}}})
                note(f"상태 '{open_name}' 처리(접수 시작): {title[:40]}")
                status = open_name

            if deadline:
                d = (datetime.strptime(deadline[:10], "%Y-%m-%d").date() - today).days
                if 0 <= d <= 7:
                    items.append((d, title, deadline[:10], kind))
            if status == "예정" and start:
                sd = (datetime.strptime(start, "%Y-%m-%d").date() - today).days
                if 0 <= sd <= 7:
                    upcoming.append((sd, title, start))

    # ⏰ 접수 마감 임박
    if items:
        items.sort()
        parts = []
        for d, title, dl, kind in items[:5]:
            icon = "🎓" if kind == "edu" else "🏆"
            tag = "(오늘!)" if d == 0 else ("(내일!)" if d == 1 else f"(D-{d})")
            parts.append(f"{icon} {title} 마감 {dl[5:].replace('-', '/')} {tag}")
        more = f" · 외 {len(items) - 5}건" if len(items) > 5 else ""
        body = " · ".join(parts) + more
    else:
        body = "없음"
    set_callout(BLK_CALLOUT_DEADLINE, "⏰ 접수 마감 임박 (7일 이내)", " — " + body)
    note(f"마감 임박 콜아웃: {len(items)}건")

    # 📌 접수 시작 예정
    if upcoming:
        upcoming.sort()
        parts = [
            f"{t} 접수 시작 {s[5:].replace('-', '/')}"
            + (" (오늘!)" if sd == 0 else f" (D-{sd})")
            for sd, t, s in upcoming[:4]
        ]
        body = " · ".join(parts)
    else:
        body = "없음"
    set_callout(BLK_CALLOUT_UPCOMING, "📌 접수 시작 예정 (7일 이내)", " — " + body)
    note(f"접수 시작 예정 콜아웃: {len(upcoming)}건")


# ---------- 6. 주간 회고 / 신체 측정 ----------

def weekly_retro(today):
    monday = today
    sunday = today + timedelta(days=6)
    # 기존 행 'W3 (7/13~7/19)' 기준 역산 → W1은 2026-06-29 주.
    week_no = (monday - date(2026, 6, 29)).days // 7 + 1
    title = f"W{week_no} ({monday.month}/{monday.day}~{sunday.month}/{sunday.day})"
    if any(prop_title(p, "주차") == title for p in query_all(RETRO_DB)):
        note(f"주간 회고 '{title}' 이미 있음")
        return
    api("/pages", "POST", {
        "parent": {"database_id": RETRO_DB},
        "properties": {
            "주차": {"title": [{"text": {"content": title}}]},
            "기간": {"date": {"start": monday.isoformat(), "end": sunday.isoformat()}},
        },
    })
    note(f"주간 회고 행 생성: {title}")


def body_measure(today):
    title = f"{today.isoformat()} (토)"
    if any(prop_title(p, "기록") == title for p in query_all(BODY_DB)):
        note(f"신체 측정 '{title}' 이미 있음")
        return
    api("/pages", "POST", {
        "parent": {"database_id": BODY_DB},
        "properties": {
            "기록": {"title": [{"text": {"content": title}}]},
            "날짜": {"date": {"start": today.isoformat()}},
        },
    })
    note(f"신체 측정 행 생성: {title} (체중·허리둘레는 직접 입력)")


# ---------- main ----------

def main():
    now = datetime.now(KST)
    today = now.date()
    weekday = today.weekday()
    yesterday = (today - timedelta(days=1)).isoformat()

    # 1 → 2 순서 필수: 체크박스를 지우기 전에 어제 취침 여부를 읽어야 한다.
    slept = read_sleep_checkbox()
    record_sleep(yesterday, slept)

    reset_schedule(today.isoformat(), weekday)
    update_dday(today)
    sync_and_callouts(today)

    if weekday == 0:
        weekly_retro(today)
    if weekday == 5:
        body_measure(today)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## 🌅 일일 루틴 — {today} ({WEEKDAY_KR[weekday]})\n\n")
            f.write(f"어제 취침: **{'성공 ✅' if slept else '실패 ❌' if slept is not None else '확인 불가'}**\n\n")
            for line in log:
                f.write(f"- {line}\n")


if __name__ == "__main__":
    main()
