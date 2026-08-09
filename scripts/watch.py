"""반도체 교육·공모전 공고 통합 감시 → 노션 자동 등록.

GitHub Actions에서 매일 실행. 클로드 클라우드 루틴은 아웃바운드가 프록시로 전면 차단돼
(구글조차 CONNECT 403) 교육 사이트를 직접 못 읽는다. GitHub 러너는 제약이 없으므로
여기서 원본 사이트를 직접 파싱한다.

소스 → 목적지:
  ISRC (JSON)        → 교육·강의 DB
  IDEC (HTML 테이블)  → 교육·강의 DB
  wevity (HTML 목록)  → 공모전·경진대회 DB   ※ 과학/공학 분야, 상세에서 마감일 확인

중복 판정은 제목이 아니라 '주요 일정'에 심는 [SRC:<소스>:<id>] 마커로 한다.
회차명이 바뀌어도 중복 등록되지 않는다.

환경변수:
  NOTION_TOKEN  노션 내부 통합 시크릿 (DB ID는 notion.py에 상수로 있다)
"""

import html
import os
import re
import sys
import time
import urllib.error
from datetime import datetime

from notion import (
    CALENDAR_DB,
    CONTEST_DB,
    EDU_DB,
    KST,
    WEEKDAY_KR,
    api as notion,
    http,
    prop_select,
    prop_text,
    prop_title,
    query_all,
)

DRY = "--dry-run" in sys.argv  # 쓰기 없이 수집 결과만 출력


def strip_tags(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def fetch(url, tries=2):
    """페이지 HTML을 가져온다. 타임아웃이면 한 번 더 시도한다.

    IDEC이 열흘에 서너 번 urlopen timeout으로 통째로 날아가서(그날 IDEC 수집 0건),
    목록 페이지처럼 실패하면 소스 전체를 잃는 요청에만 쓴다.
    """
    for n in range(tries):
        try:
            return http(url, raw=True)
        except Exception as e:
            if n == tries - 1:
                raise
            print(f"[재시도] {url} — {type(e).__name__} {e}", file=sys.stderr)
            time.sleep(3)


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


# IDEC 수강신청은 목록이 둘로 나뉜다. lst2(아카데미)에 공정·패키징 강좌가 몰려 있어
# 둘 다 긁는다. 센터명에 '-아카데미'가 붙어 있어 결과에서 구분된다.
IDEC_LISTS = (
    ("https://www.idec.or.kr/edu/apply/list/", "석·박사 우선"),
    ("https://www.idec.or.kr/edu/apply/lst2/", "재직자 우선(아카데미)"),
)

# 2학기 개강일. 평일 대면·실시간 교육은 이 날부터 수업과 겹쳐 못 간다.
SEMESTER_START = "2026-09-01"
# 학적상 학년. 2026-2까지 3학년, 2027학년도부터 4학년(전공 48/60 → 4학년에 12학점).
MY_GRADE = 3


def attend_warnings(target, edu_s):
    """실제로 못 듣는 강좌를 노트 앞에 표시한다. 판단 근거는 남기고 등록은 유지한다.

    IDEC 수강대상은 '권장'이라 표기와 실제 신청 가능 여부가 자주 어긋난다
    (3학년이 '학부 4학년 이상' 강좌를 신청하는 건 되고,
     '학부생(졸업생 이상만)'처럼 학부를 적어놓고 재학생을 빼기도 한다).
    그래서 자동으로 빼지 않고 경고만 붙인다.
    """
    warn = []
    if target:
        if "학부" not in target:
            warn.append("⚠️학부생 대상 아님")
        elif "졸업생" in target:
            warn.append("⚠️학부 재학생 제외(졸업생 이상)")
        else:
            # '학부 4학년 이상' 같은 표기. 권장일 뿐이라 빼지는 않고 표시만 한다.
            g = re.search(r"학부\s*(\d)\s*(?:학년|년차)", target)
            if g and int(g.group(1)) > MY_GRADE:
                warn.append(f"⚠️학부 {g.group(1)}학년 이상(현재 {MY_GRADE}학년)")
    if edu_s >= SEMESTER_START:
        warn.append("⚠️2학기 개강 후 — 참석 불가")
    return warn


def from_idec(today):
    """IDEC 단기교육 수강신청 목록(HTML 테이블) 두 종류.

    컬럼: 센터명 | 강의형태 | 강의제목 | 강의일자+신청기간 | 상태
    신청기간이 강의일자 뒤에 붙어 한 셀에 들어 있어 날짜 2쌍을 순서로 구분한다.

    목록의 신청기간은 '우선신청 + 전체신청'을 합친 구간이라 학부·비재직자가 실제로
    신청을 넣을 수 있는 날과 다르다(예: 목록 08-04~08-13 = 재직자 08-04~08-10 +
    전체 08-11~08-13). 진짜 시작일은 상세의 '전 체' 구간에서 뽑는다.
    끝나는 날은 둘이 같아서 마커·마감은 목록 값을 그대로 쓴다(기존 등록분과 호환).
    """
    out = []
    for list_url, kind in IDEC_LISTS:
        raw = fetch(list_url)
        # tr 여는 태그까지 통째로 잡는다 — 강좌번호(no=)가 <tr onclick=...>에 들어 있다.
        for tr in re.findall(r"<tr[^>]*>.*?</tr>", raw, re.S):
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
            # 상세 페이지에서 수강대상과 '전 체' 신청구간을 뽑는다.
            # (IDEC 강좌는 대부분 "학부3학년 이상"이지만 "석박사 우선" 표기가 헷갈려서 명시한다)
            target, whole, priority_only = "", None, False
            no = re.search(r"no=(\d+)", tr)
            if no:
                try:
                    det = strip_tags(http(f"https://www.idec.or.kr/edu/apply/view/?&no={no.group(1)}", raw=True))
                    m = re.search(r"수강대상\s*(.+?)\s*(?:사전지식|선수과목|강의목)", det)
                    if m:
                        # 40자로 자르면 뒤에 붙는 단서를 놓친다. 광운대 '첨단 반도체 재료 및
                        # 소자 설계'가 딱 잘린 자리 뒤에서 '학부생(졸업생 이상만)'이라고 못 박는다.
                        target = m.group(1).strip()[:120]
                    w = re.search(r"전\s*체\s*:\s*(\d{4}-\d{2}-\d{2})[^~]*~\s*(\d{4}-\d{2}-\d{2})", det)
                    if w:
                        whole = w.group(1, 2)
                    elif "신청 및 취소기간" in det:
                        # 우선대상 전용 강좌(전체 구간 없음) — 학부생은 신청 자체가 안 된다.
                        priority_only = True
                except Exception:
                    pass  # 상세를 못 읽으면 목록 정보만으로 등록한다
            # 전체 구간이 없거나 시작>끝으로 뒤집혀 있으면(고려대 아카데미가 이렇게 낸다)
            # 비재직자·학부생은 신청 창이 아예 안 열린다 — 등록해봐야 못 넣는다.
            if priority_only or (whole and whole[0] > whole[1]):
                continue
            if whole and whole[1] < today:
                continue
            warn = attend_warnings(target, edu_s)
            out.append({
                "target": "edu",
                # IDEC은 안정적인 강좌 ID가 노출되지 않아 센터+제목+신청마감으로 식별자를 만든다.
                "marker": f"IDEC:{center}-{title}-{app_e}"[:90],
                "title": f"{title} ({center})",
                "core_title": title,
                "deadline": app_e,
                "status": "신청중" if "신청중" in status else "예정",
                "link": list_url,
                "note": (
                    f"{' '.join(warn) + ' · ' if warn else ''}"
                    f"신청 {app_s} ~ {app_e} · 교육 {edu_s} ~ {edu_e} · {center}"
                    f"{f' · 학부·비재직자 신청 {whole[0]} ~ {whole[1]}' if whole else ''}"
                    f"{f' · 대상: {target}' if target else ''}"
                    f" · IDEC 상태:{status} · ⚠️수강신청 정오 오픈·선착순({kind})"
                ),
            })
    return out


# 위비티 분야코드 22 = 과학/공학. 예전엔 20(웹/모바일)을 반도체 키워드로 걸렀는데
# 목록 제목에 '반도체'가 들어가는 공모전이 거의 없어 매일 0건이었다(2026-08-09 수정).
# 하드웨어·임베디드·경진대회는 전부 22에 모이므로 카테고리 자체를 신뢰하고,
# 나이·신분 때문에 애초에 못 나가는 것만 걸러낸다.
WEVITY_LIST = "https://www.wevity.com/?c=find&s=1&gub=1&cidx=22"
WEVITY_PAGES = 2          # 목록 1페이지 15건. 2페이지면 접수중인 건 대부분 덮는다
WEVITY_MAX_DETAIL = 25    # 상세 조회 상한 (1건당 1요청)

# 제목만 봐도 대상이 아닌 것 — 상세를 볼 필요도 없다
WEVITY_SKIP_TITLE = ("청소년", "중학생", "고교생", "고등학생", "초등", "어린이", "유아", "학부모", "교원", "교사")
# 상세 '응모대상'은 고정 분류(대학생/일반인/청소년/어린이/제한없음/기타)다.
# '기타'는 기업 공모전이 흔히 쓰는 값이라 살려야 한다 — 초·중·고 전용만 걸러낸다.
WEVITY_TARGET_KIDS = ("청소년", "어린이", "초등")


def wevity_detail(ix):
    """상세페이지의 정보 테이블을 dict로. 목록엔 D-day뿐이라 실제 마감일은 여기 있다."""
    raw = fetch(f"{WEVITY_LIST}&gbn=view&ix={ix}")
    m = re.search(r'<ul class="cd-info-list">(.*?)</ul>', raw, re.S)
    if not m:
        return {}
    info = {}
    for li in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S):
        t = strip_tags(li)
        for key in ("접수기간", "응모대상", "주최/주관", "1등 상금", "총 상금", "홈페이지"):
            if t.startswith(key):
                info[key] = t[len(key):].strip()
                break
    return info


def from_wevity(today):
    """위비티 과학/공학 공모전 — 접수중인 것만, 상세에서 마감일·응모대상 확인."""
    out, seen, detail_calls = [], set(), 0
    for gp in range(1, WEVITY_PAGES + 1):
        raw = fetch(f"{WEVITY_LIST}&gp={gp}")
        ul = re.search(r'<ul class="list">(.*?)</ul>', raw, re.S)
        if not ul:
            continue
        for li in re.findall(r"<li[^>]*>(.*?)</li>", ul.group(1), re.S):
            ix = re.search(r"ix=(\d+)", li)
            if not ix or ix.group(1) in seen:
                continue
            text = strip_tags(li)
            # D-N만 남긴다. D+N은 이미 마감된 공고.
            if not re.search(r"\bD-\d+", text):
                continue
            # "제목 [SPECIAL] 분야 : a, b 주최사 D-25 접수중 9,789"
            title = re.split(r"\s*분야\s*:", text)[0]
            title = re.sub(r"\s*(SPECIAL|IDEA)\s*$", "", title).strip()
            if not title or any(k in title for k in WEVITY_SKIP_TITLE):
                continue
            if detail_calls >= WEVITY_MAX_DETAIL:
                continue
            seen.add(ix.group(1))
            detail_calls += 1
            info = wevity_detail(ix.group(1))
            time.sleep(0.3)

            target = info.get("응모대상", "")
            tokens = [t.strip() for t in target.split(",") if t.strip()]
            if tokens and all(any(k in t for k in WEVITY_TARGET_KIDS) for t in tokens):
                continue  # 초·중·고 전용

            period = info.get("접수기간", "")
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", period)
            deadline = dates[-1] if dates else ""
            status = "예정" if "접수예정" in text else "접수중"

            note = " · ".join(x for x in (
                "위비티",
                f"주최 {info['주최/주관']}" if info.get("주최/주관") else "",
                f"대상 {target}" if target else "",
                f"접수 {period}" if period else "",
                f"1등 {info['1등 상금']}" if info.get("1등 상금") else "",
                f"공식 {info['홈페이지']}" if info.get("홈페이지") else "",
                "" if deadline else "⚠️접수마감일 직접 확인 필요",
            ) if x)

            out.append({
                "target": "contest",
                "marker": f"WEVITY:{ix.group(1)}",
                "title": title[:120],
                "core_title": title[:120],
                "deadline": deadline,
                "status": status,
                "link": f"{WEVITY_LIST}&gbn=view&ix={ix.group(1)}",
                "note": note,
            })
    return out


KSIA_LIST = (
    "https://infra.ksia.or.kr/user/Wo/WoUser0101.do"
    "?SCH_PRM_GB=002&TAB_ID=1&CURRENT_MENU_CODE=MENU0039&TOP_MENU_CODE=MENU0039"
)
KSIA_DETAIL = (
    "https://infra.ksia.or.kr/user/Wo/WoUser0101V.do"
    "?SCH_PRM_GB=002&TAB_ID=2&CURRENT_MENU_CODE=MENU0039&TOP_MENU_CODE=MENU0039&WO_SEQ="
)


def from_ksia(today):
    """KSIA 인프라 '비재직자'(SCH_PRM_GB=002) 목록 — 학생 대상 공정교육.

    재직자 탭(001)은 학생이 못 듣지만, 비재직자 탭엔 한양대·삼성전자교수진 등
    학부생 공정교육이 올라온다. 이걸 안 봐서 한양대 특별과정을 놓쳤다(2026-07-15).

    목록: fnMoveView('SEQ','TAB') 링크 + 제목. 제목에 '(마감)'이 붙으면 종료.
    상세: WoUser0101V.do?...&WO_SEQ=SEQ 에서 날짜쌍 2개(모집/교육) + 대상 파싱.
    """
    raw = http(KSIA_LIST, raw=True)
    rows = re.findall(r"fnMoveView\('(\d+)','\d+'\);\"[^>]*>([^<]+)</a>", raw)
    out = []
    for seq, title in rows:
        title = html.unescape(title).strip()
        if "마감" in title:
            continue
        # 상세에서 날짜 확정
        try:
            detail = strip_tags(http(KSIA_DETAIL + seq, raw=True))
        except Exception:
            continue
        dates = re.findall(r"(\d{4})\.(\d{2})\.(\d{2})\s*~\s*(\d{4})\.(\d{2})\.(\d{2})", detail)
        if not dates:
            continue
        (_, _, _, ry, rm, rd) = dates[0]  # 첫 쌍 = 모집기간, 뒤 3개가 종료일
        apply_end = f"{ry}-{rm}-{rd}"
        if apply_end < today:  # 모집 마감 지남
            continue
        edu = ""
        if len(dates) >= 2:
            d = dates[1]
            edu = f"{d[0]}-{d[1]}-{d[2]} ~ {d[3]}-{d[4]}-{d[5]}"
        # 대상/자격 한 줄 (있으면)
        tgt = re.search(r"(대상|자격)[^가-힣]{0,3}([가-힣0-9()\s·,]{5,60})", detail)
        target_note = f" · 대상: {tgt.group(2).strip()}" if tgt else ""
        # 제목 뒤 괄호 안내문구(모집오픈 등)는 떼어낸다
        clean = re.sub(r"\s*\([^)]*모집[^)]*\)\s*$", "", title)
        out.append({
            "target": "edu",
            "marker": f"KSIA:{seq}",
            "title": f"{clean} (KSIA)",
            "core_title": clean,
            "deadline": apply_end,
            "status": "신청중",
            "link": KSIA_DETAIL + seq,
            "note": f"모집 ~{apply_end}{f' · 교육 {edu}' if edu else ''}{target_note} · KSIA 인프라 비재직자 · ⚠️자격요건 직접 확인",
        })
    return out


# 대학 학과 게시판 — 개별 대학 자체 모집(건국대 공정실습 등)은 ISRC·IDEC에 안 뜬다.
# 한국 대학 게시판은 대부분 같은 CMS(URL이 .../artclList.do)라 파서 하나로 여러 곳을 본다.
# (게시판명, 목록 URL, 도메인) — 관심 학과를 여기에 추가하면 된다.
UNIV_BOARDS = [
    # 반도체 실습 모집은 학과가 아니라 본부 취업/일자리 게시판에 뜬다(2026-07-14 실제 확인).
    ("건국대 취업", "https://www.konkuk.ac.kr/bbs/job/4083/artclList.do", "https://www.konkuk.ac.kr"),
]
# 제목에 이 중 하나라도 있어야 반도체 교육 공고로 본다. '실습'만으론 현장실습 등이 걸려 오탐이 난다.
UNIV_KEYWORDS = ("반도체", "공정실습", "공정 실습", "공정교육", "반도체공정", "소자공정")


def from_univ(today):
    """관심 학과 게시판에서 반도체 공정교육 모집 글만 골라낸다.

    게시판 목록엔 마감일이 없다(상세 글 본문에만 있고 형식이 제각각). 그래서
    '발견'까지만 하고 마감일·자격은 게시글 직접 확인으로 넘긴다 — 건국대를 놓친 건
    발견 자체를 못 한 것이라, 발견만 돼도 절반은 해결된다.
    """
    out = []
    for name, list_url, base in UNIV_BOARDS:
        try:
            raw = http(list_url, raw=True)
        except Exception as e:
            print(f"[대학게시판] {name} 접속 실패: {type(e).__name__} {e}", file=sys.stderr)
            continue
        for tr in re.findall(r"<tr[^>]*>.*?</tr>", raw, re.S):
            a = re.search(r'<a[^>]*href="([^"]*artclView\.do)"[^>]*>(.*?)</a>', tr, re.S)
            if not a:
                continue
            title = strip_tags(a.group(2))
            title = re.sub(r"\s*(새글|NEW)\s*$", "", title)  # 게시판 NEW 배지 제거
            if not any(k in title for k in UNIV_KEYWORDS):
                continue
            m = re.search(r"/(\d+)/artclView", a.group(1))  # 글번호 = 고유 ID
            seq = m.group(1) if m else title[:24]
            date = re.search(r"20\d\d[-.]\d\d[-.]\d\d", tr)
            # 게시일이 오래된 글은 지난 회차다 — 최근 120일 내 게시된 것만 남긴다.
            if date:
                try:
                    d = datetime.strptime(re.sub(r"[.]", "-", date.group(0)), "%Y-%m-%d").date()
                    if (datetime.now(KST).date() - d).days > 120:
                        continue
                except ValueError:
                    pass
            out.append({
                "target": "edu",
                "marker": f"UNIV:{seq}",
                "title": f"{title[:70]} ({name})",
                "core_title": title,
                "deadline": "",  # 게시판 목록엔 마감일 없음 → 게시글 직접 확인
                "status": "신청중",
                "link": base + a.group(1),
                "note": f"{name} 게시판 · 게시 {date.group(0) if date else '?'} · ⚠️마감일·자격·비용은 게시글 직접 확인 필요",
            })
    return out


# 한국나노기술원(KANC) 교육 게시판 — 반도체 단위공정(포토·식각·박막·CMP)·장비 실습 교육.
# 나노팹 보유 기관이라 IDEC(설계 위주)·ISRC가 안 잡는 '공정' 실습이 여기 뜬다.
# 게시판(css.kanc.re.kr)이라 목록엔 마감일이 없어 발견까지만 하고 마감·자격은 게시글로 넘긴다.
KANC_LIST = "https://css.kanc.re.kr/cgnb02/snb05.do"
KANC_BASE = "https://css.kanc.re.kr"
# 이 게시판은 교육 전용이지만 학점은행제 안내 등 행정글도 섞여, 교육성 키워드가 있어야 등록한다.
KANC_KEYWORDS = ("교육", "실습", "육성", "양성", "모집", "공정", "장비")


def from_kanc(today):
    """KANC 교육 게시판에서 반도체 공정·장비 교육 모집 글을 골라낸다."""
    out = []
    try:
        raw = http(KANC_LIST, raw=True)
    except Exception as e:
        print(f"[KANC] 접속 실패: {type(e).__name__} {e}", file=sys.stderr)
        return out
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", raw, re.S):
        a = re.search(r'<a[^>]*href="([^"]*mode=view[^"]*)"[^>]*>(.*?)</a>', tr, re.S)
        if not a:
            continue
        title = strip_tags(a.group(2))
        if "마감" in title:  # [마감] 접두가 붙은 글은 종료
            continue
        # 개방장비·장비사용 교육은 "해당 장비 직접 사용 계획"이 있는 실사용자 전용이라
        # (순수 교육·취업 목적 수강 불가) 학부 취업준비생에겐 부적합 → 제외.
        if any(x in title for x in ("[장비]", "개방장비", "오픈팹", "장비 사용", "장비사용")):
            continue
        if not any(k in title for k in KANC_KEYWORDS):
            continue
        cid = re.search(r"cid=(\d+)", tr)
        seq = cid.group(1) if cid else title[:24]
        date = re.search(r"20\d\d[-.]\d\d[-.]\d\d", tr)
        if date:  # 오래된 회차(120일 초과)는 지난 것으로 본다
            try:
                d = datetime.strptime(re.sub(r"[.]", "-", date.group(0)), "%Y-%m-%d").date()
                if (datetime.now(KST).date() - d).days > 120:
                    continue
            except ValueError:
                pass
        href = html.unescape(a.group(1))
        out.append({
            "target": "edu",
            "marker": f"KANC:{seq}",
            "title": f"{title[:70]} (KANC)",
            "core_title": title,
            "deadline": "",  # 게시판 목록엔 마감일 없음 → 게시글 직접 확인
            "status": "신청중",
            "link": KANC_BASE + href,
            "note": f"한국나노기술원 교육 · 게시 {date.group(0) if date else '?'} · ⚠️마감일·자격·비용은 게시글 직접 확인 필요",
        })
    return out


# 나노종합기술원(NNFC) 교육일정 — 반도체 첨단패키징 등 공정 전문교육.
# 교육 목록 페이지는 JS 렌더라 안 되지만, 그 페이지가 부르는 JSON API(list_json.do)를 직접 친다.
# 캘린더 이벤트 형식(title + start/end epoch ms)이라 접수 마감일은 없어 게시글 확인으로 넘긴다.
NNFC_JSON = "https://www.nnfc.re.kr/prog/edu/kor/sub01_03_04/list_json.do"
NNFC_VIEW = "https://www.nnfc.re.kr/prog/edu/kor/sub01_03_04/list.do"
NNFC_KEYWORDS = ("반도체", "공정", "패키징", "소자", "리소", "증착", "식각", "박막")


def from_nnfc(today):
    """NNFC 교육일정 JSON에서 반도체 공정 교육만. 교육 종료일이 지난 회차는 제외."""
    raw = http(NNFC_JSON, headers={"X-Requested-With": "XMLHttpRequest"})
    out = []
    for ev in raw if isinstance(raw, list) else []:
        title = (ev.get("title") or "").strip()
        if not any(k in title for k in NNFC_KEYWORDS):
            continue
        if "마감" in title or "완료" in title or "종료" in title:  # (모집완료) 등 접수 끝난 회차
            continue
        s, e = ev.get("start"), ev.get("end")
        edu = ""
        if s:
            sd = datetime.fromtimestamp(s / 1000, KST).strftime("%Y-%m-%d")
            ed = datetime.fromtimestamp(e / 1000, KST).strftime("%Y-%m-%d") if e else sd
            if ed < today:  # 교육이 이미 끝난 회차
                continue
            edu = f"{sd} ~ {ed}"
        out.append({
            "target": "edu",
            "marker": f"NNFC:{ev.get('_id')}",
            "title": f"{title[:70]} (NNFC)",
            "core_title": title,
            "deadline": "",  # JSON엔 교육일만 있고 접수 마감이 없음 → 게시글 확인
            "status": "예정" if edu and edu[:10] > today else "신청중",
            "link": NNFC_VIEW,
            "note": f"나노종합기술원(NNFC){f' · 교육 {edu}' if edu else ''} · ⚠️접수기간·자격·비용은 홈페이지 직접 확인 필요",
        })
    return out


# ---------- 노션 ----------

def norm(s):
    """제목 비교용 정규화 — 공백·괄호·기호 제거, 소문자."""
    return re.sub(r"[\s()\[\]<>·,.\-–—/'\"“”]+", "", s).lower()


def existing_rows(db_id, title_prop, note_prop="주요 일정"):
    """DB의 기등록 마커 집합과 정규화 제목 목록.

    마커는 이 스크립트가 심은 것만 있으므로, 그 이전에 사람이/클로드가 넣은 행은
    제목으로만 식별할 수 있다. 둘 다 써서 중복을 막는다.
    (공유용 DB는 마커를 '안내' 열에 심으므로 note_prop로 열 이름을 받는다.)
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
                t["plain_text"] for t in props.get(note_prop, {}).get("rich_text", [])
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


def class_periods(note):
    """메모 문자열에서 '실제로 그 자리에 있어야 하는' 날짜 구간만 뽑는다.

    같은 메모에 신청기간과 교육기간이 섞여 있어(신청 08-04~08-13 · 교육 08-18~08-19)
    날짜쌍을 통째로 긁으면 신청창까지 일정으로 오해한다. '·'로 끊어서
    신청·모집·접수가 붙은 조각은 버린다. ISRC처럼 이론/실습이 나뉜 행은 둘 다 잡힌다.
    """
    out = []
    for seg in note.split("·"):
        if any(k in seg for k in ("신청", "모집", "접수")):
            continue
        out.extend(re.findall(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", seg))
    return out


def booked_periods():
    """'✅ 신청완료' 행이 잡아먹은 날짜 구간 — [(교육명, 시작, 끝), ...]."""
    out, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = notion(f"/databases/{EDU_DB}/query", method="POST", data=payload)
        for page in res["results"]:
            props = page["properties"]
            if (props.get("상태", {}).get("select") or {}).get("name") != "✅ 신청완료":
                continue
            title = "".join(t["plain_text"] for t in props.get("교육명", {}).get("title", []))
            note = "".join(t["plain_text"] for t in props.get("주요 일정", {}).get("rich_text", []))
            out.extend((title, s, e) for s, e in class_periods(note))
        if not res.get("has_more"):
            return out
        cursor = res["next_cursor"]


def conflicts(item, booked):
    """이미 신청한 교육과 날짜가 겹치는지. 겹치면 경고 문구를 만든다."""
    hits = []
    for s, e in class_periods(item["note"]):
        for title, bs, be in booked:
            if s <= be and bs <= e:  # 구간 교집합
                hits.append(f"{title}({bs[5:]}~{be[5:]})")
    # 이론/실습처럼 한 강좌가 여러 구간이면 같은 이름이 겹쳐 잡힌다
    hits = list(dict.fromkeys(hits))
    return f"⚠️충돌: {', '.join(hits)}" if hits else ""


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


# 공유용 DB — 개인 신청 상태·개인 메모 없이 순수 교육 정보만. 웹 게시해서 공유한다.
SHARE_DB = "ed571515fdeb4af69f8df0e46b7121a0"


def add_share_row(item):
    """공유용 DB에 교육 정보를 넣는다. 개인 DB와 달리 사람이 손댄 개인 상태가 없어
    그대로 공개해도 안전하다. 마커는 '안내' 열에 심어 중복을 막는다."""
    note = f"{item['note']} · [SRC:{item['marker']}]"
    props = {
        "교육명": {"title": [{"text": {"content": item["title"][:200]}}]},
        "상태": {"select": {"name": item["status"]}},
        "분야": {"select": {"name": classify(item["title"])}},
        "링크": {"url": item["link"]},
        "안내": {"rich_text": [{"text": {"content": note[:2000]}}]},
    }
    iso = to_iso(item["deadline"]) if item["deadline"] else None
    if iso:
        props["신청마감"] = {"date": {"start": iso}}
    notion("/pages", method="POST", data={"parent": {"database_id": SHARE_DB}, "properties": props})


# ---------- 신청완료 교육 → 실행 캘린더 미러 ----------
# 교육 DB에서 '상태'를 손으로 '✅ 신청완료'로 바꾸면, 다음 실행 때 실행 캘린더에
# 교육 일정이 자동 추가된다. 캘린더 '메모'에 [CAL:<키>]를 심어 중복을 막는다.
APPLIED_STATUS = "✅ 신청완료"


def parse_edu_dates(note):
    """'주요 일정' 텍스트에서 교육 기간 (start, end)를 YYYY-MM-DD로 뽑는다. 없으면 (None, None).

    지원 형식:
      · 교육 2026-07-27 09:00 ~ 2026-07-28 18:00   (ISRC/IDEC/KSIA 자동수집)
      · 교육 2026-07-24                              (하루짜리)
      · 교육 7/27(월)~7/29(수)                        (사람이 쓴 자유형식, 올해 연도)
    """
    m = re.search(r"교육\s*(\d{4}-\d{2}-\d{2})[^~]*~\s*(\d{4}-\d{2}-\d{2})", note)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"교육\s*(\d{4}-\d{2}-\d{2})", note)
    if m:
        return m.group(1), m.group(1)
    m = re.search(r"교육\s*(\d{1,2})/(\d{1,2})[^~\d]*(?:~\s*(\d{1,2})/(\d{1,2}))?", note)
    if m:
        y = datetime.now(KST).year
        start = f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        end = f"{y}-{int(m.group(3)):02d}-{int(m.group(4)):02d}" if m.group(3) else start
        return start, end
    return None, None


def add_calendar_row(title, start, end, key, link, deadline):
    """실행 캘린더에 교육 일정 1건 추가."""
    clean = re.sub(r"^[✅❌]\s*", "", title).strip()
    memo = f"교육 신청완료(자동등록){f' · {link}' if link else ''} · [CAL:{key}]"
    props = {
        "할 일": {"title": [{"text": {"content": f"🎓 {clean[:180]}"}}]},
        "구분": {"select": {"name": "IDEC"}},
        "메모": {"rich_text": [{"text": {"content": memo[:2000]}}]},
    }
    # 교육 날짜가 있으면 그걸, 없으면 신청마감일이라도 넣어 캘린더에서 보이게 한다.
    d_start, d_end = (start, end) if start else (deadline, None)
    if d_start:
        date = {"start": d_start}
        if d_end and d_end != d_start:
            date["end"] = d_end
        props["날짜"] = {"date": date}
        try:
            wd = datetime.strptime(d_start, "%Y-%m-%d").weekday()
            props["요일"] = {"rich_text": [{"text": {"content": WEEKDAY_KR[wd]}}]}
        except ValueError:
            pass
    notion("/pages", method="POST", data={"parent": {"database_id": CALENDAR_DB}, "properties": props})


def mirror_applied_to_calendar():
    """교육 DB에서 상태='✅ 신청완료'인 행을 실행 캘린더에 미러. 추가한 (제목, 시작, 끝) 목록 반환."""
    cal_keys = set()
    for pg in query_all(CALENDAR_DB):
        cal_keys.update(re.findall(r"\[CAL:([^\]]+)\]", prop_text(pg, "메모")))

    added = []
    for pg in query_all(EDU_DB):
        if prop_select(pg, "상태") != APPLIED_STATUS:
            continue
        note = prop_text(pg, "주요 일정")
        m = re.search(r"\[SRC:([^\]]+)\]", note)
        key = m.group(1) if m else f"EDU:{pg['id'][:8]}"
        if key in cal_keys:
            continue
        title = prop_title(pg, "교육명")
        start, end = parse_edu_dates(note)
        link = pg["properties"].get("링크", {}).get("url") or ""
        deadline = (pg["properties"].get("신청마감", {}).get("date") or {}).get("start", "")
        deadline = deadline[:10] if deadline else ""
        if not DRY:
            add_calendar_row(title, start, end, key, link, deadline)
        cal_keys.add(key)
        added.append((title, start, end))
    return added


# ---------- 만료된 미신청 교육 정리 ----------
# 상태가 '✅ 신청완료'가 아닌 교육 중 신청마감 또는 교육이 지난 것을 노션 휴지통으로 보낸다.
# (신청완료는 참여 기록이라 영구 보존, 날짜를 알 수 없는 항목은 판단 불가라 보존.)
def _passed(dstr, today):
    return bool(dstr) and dstr[:10] < today


# 참여·이수 흔적이 있으면(상태 외에 제목/메모로 표시한 경우까지) 만료돼도 보존한다.
KEEP_RE = re.compile(r"✅|수강\s*완료|신청\s*완료|참여\s*완료|이수|수료")


def cleanup_expired(today):
    """만료된 미신청 교육을 휴지통으로. 삭제한 (제목, 사유) 목록 반환."""
    removed = []
    for pg in query_all(EDU_DB):
        title = prop_title(pg, "교육명")
        note = prop_text(pg, "주요 일정")
        if prop_select(pg, "상태") == APPLIED_STATUS or KEEP_RE.search(title) or KEEP_RE.search(note):
            continue  # 신청완료·참여이수 기록은 보존
        deadline = (pg["properties"].get("신청마감", {}).get("date") or {}).get("start", "")
        _, edu_end = parse_edu_dates(prop_text(pg, "주요 일정"))
        reason = ""
        if _passed(deadline, today):
            reason = f"신청마감 {deadline[:10]} 지남"
        elif _passed(edu_end, today):
            reason = f"교육 {edu_end} 지남"
        if reason:
            if not DRY:
                notion(f"/pages/{pg['id']}", method="PATCH", data={"archived": True})
            removed.append((title, reason))
    return removed


# ---------- main ----------

def main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    items, errors = [], []

    for name, fn in (("ISRC", from_isrc), ("IDEC", from_idec), ("KSIA", from_ksia), ("KANC", from_kanc), ("NNFC", from_nnfc), ("wevity", from_wevity), ("대학게시판", from_univ)):
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

    try:
        booked = booked_periods()
    except Exception as e:  # 충돌 표시는 부가 정보라 실패해도 등록은 계속한다
        errors.append(f"신청완료 일정 조회 실패: {type(e).__name__} {e}")
        booked = []

    new = []
    for i in items:
        if i["target"] == "contest" and contest_titles is None:
            continue
        titles = edu_titles if i["target"] == "edu" else contest_titles
        markers = edu_markers if i["target"] == "edu" else contest_markers
        if not is_known(i, markers, titles):
            if i["target"] == "edu" and (c := conflicts(i, booked)):
                i["note"] = f"{c} · {i['note']}"
            new.append(i)

    print(f"\n[{today}] 수집 {len(items)}건 / 신규 {len(new)}건" + (" (dry-run: 등록 안 함)" if DRY else ""))
    for i in new:
        if not DRY:
            add_row(i)
        print(f"  {'· 미리보기' if DRY else '→ 등록'}: [{i['target']}] {i['title']} (마감 {i['deadline'] or '확인필요'})")

    # 공유용 DB 미러링 — 교육 항목만, 개인 정보 없이. 통합 연결 안 됐으면 조용히 건너뛴다.
    try:
        share_markers, share_titles = existing_rows(SHARE_DB, "교육명", note_prop="안내")
        share_new = [i for i in items if i["target"] == "edu" and not is_known(i, share_markers, share_titles)]
        for i in share_new:
            if not DRY:
                add_share_row(i)
        print(f"[공유용] 신규 {len(share_new)}건" + (" (dry-run)" if DRY else " 미러링"))
    except urllib.error.HTTPError as e:
        errors.append(f"공유용 DB 접근 실패(통합 연결 확인): HTTP {e.code}")
        print(f"[공유용] 통합 연결 안 됨 (HTTP {e.code}) — 개인 DB는 정상 처리됨", file=sys.stderr)

    # 신청완료 교육 → 실행 캘린더 미러 (사람이 상태를 '✅ 신청완료'로 바꾼 건만)
    try:
        cal_added = mirror_applied_to_calendar()
        for t, s, e in cal_added:
            span = s if s == e or not e else f"{s}~{e}"
            print(f"  {'· 미리보기' if DRY else '→ 캘린더'}: {t} ({span or '날짜미상'})")
        print(f"[캘린더] 신청완료 미러 {len(cal_added)}건" + (" (dry-run)" if DRY else ""))
    except urllib.error.HTTPError as e:
        errors.append(f"캘린더 미러 실패: HTTP {e.code}")
        print(f"[캘린더] 실패 (HTTP {e.code})", file=sys.stderr)

    # 만료된 미신청 교육 정리 (상태!=신청완료 & 신청마감/교육 지남 → 휴지통)
    try:
        removed = cleanup_expired(today)
        for t, why in removed:
            print(f"  {'· 삭제예정' if DRY else '🗑 삭제'}: {t[:40]} ({why})")
        print(f"[정리] 만료 미신청 {len(removed)}건" + (" (dry-run)" if DRY else " 휴지통"))
    except urllib.error.HTTPError as e:
        errors.append(f"정리 실패: HTTP {e.code}")
        print(f"[정리] 실패 (HTTP {e.code})", file=sys.stderr)

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
