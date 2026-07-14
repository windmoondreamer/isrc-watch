"""반도체 교육 공고 감시 → 노션 교육·강의 DB 자동 등록.

GitHub Actions에서 매일 실행. 클라우드 루틴(Claude)의 네트워크 제한을 우회하려고
GitHub 러너에서 직접 소스를 조회한다.

소스:
  - 서울대 ISRC 교육시스템 일정 JSON (모집중/예정 과정 전부)

동작:
  1. ISRC 일정 JSON 조회 → 접수종료일이 오늘 이후인 과정 추출 (EVENT_SKEY 중복 제거)
  2. 노션 교육·강의 DB 조회 → 이미 등록된 과정 파악 (주요 일정의 [ISRC:<skey>] 마커로 식별)
  3. 미등록 과정만 DB에 행 추가
  4. 추가한 게 있으면 GitHub Actions Summary에 출력 (+ 종료코드로 알림)

환경변수:
  NOTION_TOKEN  - 노션 내부 통합 시크릿 (ntn_...)
  NOTION_DB_ID  - 교육·강의 데이터베이스 ID
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ISRC_URL = "https://isrc.snu.ac.kr/edu/school/info/state/json"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

TOKEN = os.environ["NOTION_TOKEN"]
DB_ID = os.environ["NOTION_DB_ID"]


def http(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, body, timeout=30) as resp:
        return json.loads(resp.read().decode())


def notion(path, method="GET", data=None):
    return http(
        f"{NOTION_API}{path}",
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Notion-Version": NOTION_VERSION,
        },
    )


def fetch_isrc(today):
    """접수종료일이 오늘 이후인 ISRC 과정 목록. EVENT_SKEY 기준 중복 제거."""
    raw = http(
        ISRC_URL,
        method="POST",
        data={},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    courses = {}
    for row in raw.get("data", []):
        end = (row.get("RECEIVE_END_DT_STR") or "")[:10]
        if not end or end < today:
            continue
        name = (row.get("NAME") or "").strip()
        # (일반) = 재직자 전용 트랙. 같은 교육의 (학생) 트랙이 따로 열리므로 학부생에겐 노이즈.
        if "(일반)" in name:
            continue
        skey = row.get("EVENT_SKEY")
        if skey in courses:
            continue
        courses[skey] = {
            "skey": skey,
            "name": name,
            "apply_start": row.get("RECEIVE_START_DT_STR", ""),
            "apply_end": row.get("RECEIVE_END_DT_STR", ""),
            "edu_start": row.get("START_DT_STR", ""),
            "edu_end": row.get("END_DT_STR", ""),
            "status": row.get("STATUS", ""),
        }
    return list(courses.values())


def existing_skeys():
    """노션 DB에 이미 등록된 ISRC 과정의 EVENT_SKEY 집합.

    '주요 일정' 텍스트에 심어둔 [ISRC:<skey>] 마커로 식별한다.
    과정명은 회차마다 바뀌므로 이름 매칭은 쓰지 않는다.
    """
    found = set()
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = notion(f"/databases/{DB_ID}/query", method="POST", data=payload)
        for page in res["results"]:
            prop = page["properties"].get("주요 일정", {})
            text = "".join(t["plain_text"] for t in prop.get("rich_text", []))
            if "[ISRC:" in text:
                marker = text.split("[ISRC:", 1)[1].split("]", 1)[0]
                found.add(int(marker))
        if not res.get("has_more"):
            return found
        cursor = res["next_cursor"]


def to_iso(dt_str):
    """'2026-07-20 09:00' → '2026-07-20T09:00:00+09:00'"""
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=KST).isoformat()
    except ValueError:
        return None


def add_course(c):
    note_parts = [
        f"접수 {c['apply_start']} ~ {c['apply_end']}",
        f"교육 {c['edu_start']} ~ {c['edu_end']}",
        "자격요건·비용 확인 필요",
        f"출처: ISRC 교육시스템 [ISRC:{c['skey']}]",
    ]

    props = {
        "교육명": {"title": [{"text": {"content": c["name"][:200]}}]},
        "상태": {"select": {"name": "신청중" if c["status"] == "신청" else "예정"}},
        "분야": {"select": {"name": "공정"}},
        "링크": {"url": "https://isrc.snu.ac.kr/edu/school/event/list"},
        "주요 일정": {"rich_text": [{"text": {"content": " · ".join(note_parts)[:2000]}}]},
    }
    iso = to_iso(c["apply_end"])
    if iso:
        props["신청마감"] = {"date": {"start": iso}}

    notion(
        "/pages",
        method="POST",
        data={"parent": {"database_id": DB_ID}, "properties": props},
    )


def main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    courses = fetch_isrc(today)
    known = existing_skeys()
    new = [c for c in courses if c["skey"] not in known]

    print(f"[{today}] ISRC 모집중·예정 {len(courses)}건 / 노션 기등록 {len(known)}건 / 신규 {len(new)}건")
    for c in courses:
        mark = "NEW" if c["skey"] in {n["skey"] for n in new} else "   "
        print(f"  {mark} [{c['skey']}] {c['name']} (접수 ~{c['apply_end']})")

    for c in new:
        add_course(c)
        print(f"  → 노션 추가: {c['name']}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            if new:
                f.write(f"## 🆕 신규 교육 공고 {len(new)}건 — 노션 등록 완료\n\n")
                for c in new:
                    f.write(f"- **{c['name']}**\n")
                    f.write(f"  - 접수: {c['apply_start']} ~ {c['apply_end']}\n")
                    f.write(f"  - 교육: {c['edu_start']} ~ {c['edu_end']}\n")
            else:
                f.write(f"## ✅ 신규 공고 없음 (모집중·예정 {len(courses)}건 전부 기등록)\n")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        sys.exit(1)
