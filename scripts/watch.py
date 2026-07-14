"""반도체 교육·공모전 공고 통합 감시 → 노션 자동 등록.

GitHub Actions에서 매일 실행. 클로드 클라우드 루틴은 아웃바운드가 프록시로 전면 차단돼
(구글조차 CONNECT 403) 교육 사이트를 직접 못 읽는다. GitHub 러너는 제약이 없으므로
여기서 원본 사이트를 직접 파싱한다.

소스 → 목적지:
  ISRC (JSON)        → 교육·강의 DB
  IDEC (HTML 테이블)  → 교육·강의 DB
  wevity (HTML 목록)  → 공모전·경진대회 DB   ※ 반도체 키워드 필터

중복 판정은 제목이 아니라 '주요 일정'에 심는 [SRC:<소스>:<id>] 마커로 한다.
회차명이 바뀌어도 중복 등록되지 않는다.

환경변수:
  NOTION_TOKEN      노션 내부 통합 시크릿
  NOTION_EDU_DB     교육·강의 DB ID
  NOTION_CONTEST_DB 공모전·경진대회 DB ID
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

TOKEN = os.environ["NOTION_TOKEN"]
EDU_DB = os.environ["NOTION_EDU_DB"]
CONTEST_DB = os.environ["NOTION_CONTEST_DB"]

# 공모전 목록에서 반도체 관련만 걸러내는 키워드
SEMI_KEYWORDS = ("반도체", "회로설계", "FPGA", "Verilog", "RTL", "소자", "칩설계", "SoC")


# ---------- HTTP ----------

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


def notion(path, method="GET", data=None):
    return http(
        f"{NOTION_API}{path}",
        method=method,
        data=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Notion-Version": NOTION_VERSION},
    )


def strip_tags(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


# ---------- 소스별 수집 ----------

def from_isrc(today):
    """서울대 ISRC 교육시스템 일정 JSON. 접수종료일이 오늘 이후인 학생 대상 과정."""
    raw = http(
        "https://isrc.snu.ac.kr/edu/school/info/state/json",
        method="POST",
        data={},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    out = {}
    for row in raw.get("data", []):
        end = (row.get("RECEIVE_END_DT_STR") or "")[:10]
        if not end or end < today:
            continue
        name = (row.get("NAME") or "").strip()
        # (일반) = 재직자 전용 트랙. 같은 교육의 (학생) 트랙이 따로 열리므로 학부생에겐 노이즈.
        if "(일반)" in name:
            continue
        skey = row.get("EVENT_SKEY")
        if skey in out:
            continue
        out[skey] = {
            "target": "edu",
            "marker": f"ISRC:{skey}",
            "title": name,
            "core_title": name,
            "deadline": row.get("RECEIVE_END_DT_STR", ""),
            "status": "신청중" if row.get("STATUS") == "신청" else "예정",
            "link": "https://isrc.snu.ac.kr/edu/school/event/list",
            "note": (
                f"접수 {row.get('RECEIVE_START_DT_STR','')} ~ {row.get('RECEIVE_END_DT_STR','')} · "
                f"교육 {row.get('START_DT_STR','')} ~ {row.get('END_DT_STR','')} · "
                f"주관 서울대 ISRC · 자격요건·비용 확인 필요"
            ),
        }
    return list(out.values())


def from_idec(today):
    """IDEC 단기교육 수강신청 목록(HTML 테이블).

    컬럼: 센터명 | 강의형태 | 강의제목 | 강의일자+신청기간 | 상태
    신청기간이 강의일자 뒤에 붙어 한 셀에 들어 있어 날짜 2쌍을 순서로 구분한다.
    """
    raw = http("https://www.idec.or.kr/edu/apply/list/", raw=True)
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
        cells = [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if len(cells) < 5 or cells[0] == "센터명":
            continue
        center, _fmt, title, dates, status = cells[0], cells[1], cells[2], cells[3], cells[4]
        found = re.findall(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", dates)
        if len(found) < 2:
            continue
        (edu_s, edu_e), (app_s, app_e) = found[0], found[1]
        if app_e < today:  # 신청 마감 지난 강좌는 스킵
            continue
        out.append({
            "target": "edu",
            # IDEC은 안정적인 강좌 ID가 노출되지 않아 센터+제목+신청마감으로 식별자를 만든다.
            "marker": f"IDEC:{center}-{title}-{app_e}"[:90],
            "title": f"{title} ({center})",
            "core_title": title,
            "deadline": app_e,
            "status": "신청중" if "신청중" in status else "예정",
            "link": "https://www.idec.or.kr/edu/apply/list/",
            "note": (
                f"신청 {app_s} ~ {app_e} · 교육 {edu_s} ~ {edu_e} · "
                f"{center} · IDEC 상태:{status} · ⚠️수강신청 정오 오픈·선착순"
            ),
        })
    return out


def from_wevity(today):
    """위비티 과학/공학 공모전 목록에서 반도체 관련만."""
    raw = http("https://www.wevity.com/?c=find&s=1&gub=1&cidx=20", raw=True)
    out = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", raw, re.S):
        text = strip_tags(li)
        if "D-" not in text or len(text) < 20:
            continue
        if not any(k in text for k in SEMI_KEYWORDS):
            continue
        ix = re.search(r"ix=(\d+)", li)
        if not ix:
            continue
        # "D-12 SPECIAL 제목 분야1, 분야2" → 제목만 남기기 어려워 앞부분을 그대로 쓴다.
        title = re.sub(r"^D-\d+\s*(SPECIAL\s*)?", "", text)[:80]
        out.append({
            "target": "contest",
            "marker": f"WEVITY:{ix.group(1)}",
            "title": title,
            "core_title": title,
            "deadline": "",  # 목록엔 D-day만 있어 정확한 날짜는 상세 확인 필요
            "status": "접수중",
            "link": f"https://www.wevity.com/?c=find&s=1&gub=1&cidx=20&gbn=view&ix={ix.group(1)}",
            "note": f"위비티 발견 · {text[:100]} · ⚠️접수마감일·자격요건 직접 확인 필요",
        })
    return out


# ---------- 노션 ----------

def norm(s):
    """제목 비교용 정규화 — 공백·괄호·기호 제거, 소문자."""
    return re.sub(r"[\s()\[\]<>·,.\-–—/'\"“”]+", "", s).lower()


def existing_rows(db_id, title_prop):
    """DB의 기등록 마커 집합과 정규화 제목 목록.

    마커는 이 스크립트가 심은 것만 있으므로, 그 이전에 사람이/클로드가 넣은 행은
    제목으로만 식별할 수 있다. 둘 다 써서 중복을 막는다.
    """
    markers, titles = set(), []
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = notion(f"/databases/{db_id}/query", method="POST", data=payload)
        for page in res["results"]:
            props = page["properties"]
            note = "".join(
                t["plain_text"] for t in props.get("주요 일정", {}).get("rich_text", [])
            )
            markers.update(re.findall(r"\[SRC:([^\]]+)\]", note))
            # 초기 도입기 마커 형식([ISRC:2514])도 인식
            markers.update(f"ISRC:{m}" for m in re.findall(r"\[ISRC:(\d+)\]", note))
            title = "".join(t["plain_text"] for t in props.get(title_prop, {}).get("title", []))
            if title:
                titles.append(norm(title))
        if not res.get("has_more"):
            return markers, titles
        cursor = res["next_cursor"]


def is_known(item, markers, titles):
    if item["marker"] in markers:
        return True
    # 마커 없이 이미 등록된 행 방어 — 제목이 서로 포함 관계면 같은 강좌로 본다.
    core = norm(item["core_title"])
    if len(core) < 6:  # 너무 짧은 제목은 오탐 위험이 커 마커만 믿는다
        return False
    return any(core in t or t in core for t in titles)


def classify(title):
    """교육·강의 DB의 '분야' select 값 추론. 확신 없으면 '공통'."""
    t = title.lower()
    # '소자'는 '수동소자'(RF·아날로그)까지 걸려 오분류를 내므로 넣지 않는다.
    process = ("공정", "증착", "식각", "포토", "박막", "tcad", "패키징", "package", "웨이퍼")
    digital = ("verilog", "rtl", "soc", "asic", "fpga", "디지털", "hls", "layout", "logic", "innovus", "place and route")
    analog = ("pll", "rf", "pmic", "dc-dc", "아날로그", "컨버터", "수동소자", "wireless", "트랜스시버", "mixed", "전력반도체")
    if any(k in t for k in process):
        return "공정"
    if any(k in t for k in analog):
        return "아날로그설계"
    if any(k in t for k in digital):
        return "디지털설계"
    return "공통"


def contest_type(title):
    """공모전 DB의 '방식' select 값 추론."""
    t = title.lower()
    if any(k in t for k in ("ai", "데이터", "머신러닝", "딥러닝")):
        return "AI·데이터"
    if any(k in t for k in ("설계", "회로", "칩", "verilog", "rtl")):
        return "설계"
    return "복합"


def to_iso(s):
    """'2026-07-20 09:00' 또는 '2026-07-20' → ISO8601(KST). 실패 시 None."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST).isoformat()
        except ValueError:
            continue
    return None


def add_row(item):
    note = f"{item['note']} · [SRC:{item['marker']}]"
    if item["target"] == "edu":
        db_id = EDU_DB
        props = {
            "교육명": {"title": [{"text": {"content": item["title"][:200]}}]},
            "상태": {"select": {"name": item["status"]}},
            "분야": {"select": {"name": classify(item["title"])}},
            "링크": {"url": item["link"]},
            "주요 일정": {"rich_text": [{"text": {"content": note[:2000]}}]},
        }
        date_prop = "신청마감"
    else:
        db_id = CONTEST_DB
        props = {
            "대회명": {"title": [{"text": {"content": item["title"][:200]}}]},
            "상태": {"select": {"name": item["status"]}},
            "방식": {"select": {"name": contest_type(item["title"])}},
            "링크": {"url": item["link"]},
            "주요 일정": {"rich_text": [{"text": {"content": note[:2000]}}]},
        }
        date_prop = "접수마감"

    iso = to_iso(item["deadline"]) if item["deadline"] else None
    if iso:
        props[date_prop] = {"date": {"start": iso}}

    notion("/pages", method="POST", data={"parent": {"database_id": db_id}, "properties": props})


# ---------- main ----------

def main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    items, errors = [], []

    for name, fn in (("ISRC", from_isrc), ("IDEC", from_idec), ("wevity", from_wevity)):
        try:
            got = fn(today)
            items.extend(got)
            print(f"[{name}] 수집 {len(got)}건")
        except Exception as e:  # 소스 하나가 죽어도 나머지는 계속
            errors.append(f"{name}: {type(e).__name__} {e}")
            print(f"[{name}] 실패: {e}", file=sys.stderr)

    edu_markers, edu_titles = existing_rows(EDU_DB, "교육명")
    try:
        contest_markers, contest_titles = existing_rows(CONTEST_DB, "대회명")
    except urllib.error.HTTPError as e:
        # 공모전 DB에 통합이 연결 안 됐어도 교육 쪽은 계속 처리한다.
        errors.append(f"공모전 DB 접근 실패(통합 연결 확인): HTTP {e.code}")
        contest_markers, contest_titles = set(), None

    new = []
    for i in items:
        if i["target"] == "contest" and contest_titles is None:
            continue
        titles = edu_titles if i["target"] == "edu" else contest_titles
        markers = edu_markers if i["target"] == "edu" else contest_markers
        if not is_known(i, markers, titles):
            new.append(i)

    print(f"\n[{today}] 수집 {len(items)}건 / 신규 {len(new)}건")
    for i in new:
        add_row(i)
        print(f"  → 등록: [{i['target']}] {i['title']} (마감 {i['deadline'] or '확인필요'})")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            if new:
                f.write(f"## 🆕 신규 {len(new)}건 노션 등록\n\n")
                for i in new:
                    f.write(f"- **{i['title']}**\n  - 마감: {i['deadline'] or '확인 필요'}\n  - {i['note'][:120]}\n")
            else:
                f.write(f"## ✅ 신규 없음 (수집 {len(items)}건 전부 기등록)\n")
            if errors:
                f.write("\n### ⚠️ 소스 오류\n")
                for e in errors:
                    f.write(f"- {e}\n")

    # 소스가 전부 죽으면 실패로 알린다 (일부 실패는 통과)
    if len(errors) == 3:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        sys.exit(1)
