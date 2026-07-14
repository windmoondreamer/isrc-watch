"""노션 API 얇은 래퍼 + 워크스페이스 상수.

watch.py(공고 감시)와 daily.py(일일 루틴)가 공유한다.
"""

import json
import os
import urllib.request
from datetime import timedelta, timezone

KST = timezone(timedelta(hours=9))
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

TOKEN = os.environ["NOTION_TOKEN"]

# --- 페이지 ---
MAIN_PAGE = "384d82c7252581d6aabafaa6adbc731f"

# --- 데이터베이스 ---
EDU_DB = "3244fcb7249c4098aa5a7f81a572c8cb"        # 교육·강의
CONTEST_DB = "a9e6fc4e940a43628a018bc6220c91d5"    # 공모전·경진대회
CALENDAR_DB = "7305739ebe3048d082b62d1efb0826b5"   # 실행 캘린더 (여름)
RETRO_DB = "dea6c521912844b49d5458bb557ef938"      # 주간 회고
BODY_DB = "bce0e8b176c249f5989e412660cf7ba6"       # 신체 측정

# --- 메인 페이지의 고정 블록 ID ---
# 노션 블록 ID는 블록을 지우고 새로 만들지 않는 한 바뀌지 않는다.
# 사람이 블록을 지우고 다시 만들면 여기가 깨지므로, daily.py는 텍스트 패턴으로도 검증한다.
BLK_SESSION_LINE = "24691ea3-2f6c-4eaf-a538-d584ca5ee036"      # 📆 날짜 | 💪 세션
BLK_SLEEP_TODO = "bf9d4c9c-8554-4b69-a0eb-f762edec7244"        # 🛏️ 00:00 전 취침
BLK_CALLOUT_DEADLINE = "1253b875-1a2f-4b49-b115-50ed62a743a5"  # ⏰ 접수 마감 임박
BLK_CALLOUT_UPCOMING = "42316e9f-861b-42b4-8e8a-631d93e16b8a"  # 📌 접수 시작 예정
BLK_CERT_TABLE = "7fd93219-930f-4fc9-a589-a027195a0082"        # 자격증 3관왕 표

# 자격증 표의 행 ID (헤더 제외)
CERT_ROWS = {
    "ADsP": "beaa78cd-b2c5-420c-a761-5019a038214b",
    "OPIc": "84d10280-f11d-4610-9912-3922f0d05a5e",
    "SQLD": "b074cb3a-c206-48ae-8115-efe900034706",
}

# ⚡ 지금 당장 TOP 3 안의 D-Day 표기가 있는 항목
BLK_TOP3 = {
    "ADsP": "a6654334-9c66-47c4-8060-92736b9197a8",
    "OPIc": "07ff7bd1-8274-4185-a1fd-22b5c6a1b0c9",
}

EXAMS = {"ADsP": "2026-08-08", "OPIc": "2026-08-14", "SQLD": "2026-08-22"}

SESSIONS = {
    0: "Push A",
    1: "Pull A",
    2: "Zone 2 + 자세교정",
    3: "Upper 하이퍼트로피",
    4: "Legs + Core + HIIT",
    5: "Pull B 또는 휴식",
    6: "완전 휴식",
}
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def http(url, method="GET", data=None, headers=None, raw=False):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, body, timeout=30) as resp:
        payload = resp.read().decode("utf-8", "ignore")
    return payload if raw else json.loads(payload)


def api(path, method="GET", data=None):
    return http(
        API + path,
        method=method,
        data=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Notion-Version": VERSION},
    )


def plain(rich):
    return "".join(t["plain_text"] for t in rich)


def block_text(block):
    return plain(block.get(block["type"], {}).get("rich_text", []))


def query_all(db_id):
    """DB의 모든 행."""
    rows, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = api(f"/databases/{db_id}/query", "POST", payload)
        rows.extend(res["results"])
        if not res.get("has_more"):
            return rows
        cursor = res["next_cursor"]


def prop_title(page, name):
    return plain(page["properties"].get(name, {}).get("title", []))


def prop_text(page, name):
    return plain(page["properties"].get(name, {}).get("rich_text", []))


def prop_select(page, name):
    sel = page["properties"].get(name, {}).get("select")
    return sel["name"] if sel else None


def prop_date(page, name):
    d = page["properties"].get(name, {}).get("date")
    return d["start"] if d else None
