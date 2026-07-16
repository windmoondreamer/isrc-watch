"""공유용 DB → 정적 카탈로그 사이트(docs/index.html) 빌드.

watch.py가 공유용 DB를 갱신한 뒤 실행된다. 공유용 DB엔 개인 정보가 없으므로
그대로 공개 사이트로 만든다. GitHub Pages가 docs/ 를 서빙한다.
"""

import html as H
import os
import re
from datetime import datetime

from notion import KST, api, plain

SHARE_DB = "ed571515fdeb4af69f8df0e46b7121a0"
FIELD = {"공정": "proc", "디지털설계": "digi", "아날로그설계": "ana", "공통": "comm"}


def fetch():
    today = datetime.now(KST).date()
    rows = []
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = api(f"/databases/{SHARE_DB}/query", "POST", payload)
        for p in res["results"]:
            pr = p["properties"]
            st = (pr.get("상태", {}).get("select") or {}).get("name", "")
            if st == "마감":
                continue
            title = plain(pr.get("교육명", {}).get("title", []))
            field = (pr.get("분야", {}).get("select") or {}).get("name", "")
            dl = (pr.get("신청마감", {}).get("date") or {}).get("start", "")
            note = plain(pr.get("안내", {}).get("rich_text", []))
            link = pr.get("링크", {}).get("url", "")
            dday = ""
            if dl:
                try:
                    d = datetime.fromisoformat(dl.replace("Z", "+00:00")).astimezone(KST).date()
                    dday = (d - today).days
                except ValueError:
                    pass
            rows.append(dict(title=title, status=st, field=field, deadline=dl[:10] if dl else "",
                             dday=dday, note=note, link=link))
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    rows.sort(key=lambda x: (x["deadline"] == "", x["deadline"]))
    return rows, str(today)


def summ(note):
    n = re.sub(r"\[SRC:[^\]]+\]", "", note)
    n = re.sub(r"⚠️?[^·]*", "", n)
    return " ".join(n.split())[:90].strip(" ·")


def dday_html(dd):
    if dd == "" or dd is None:
        return '<span class="dd dd-open">상시·미정</span>'
    dd = int(dd)
    cls = "dd-far"
    if dd <= 1:
        cls = "dd-now"
    elif dd <= 7:
        cls = "dd-soon"
    lab = "오늘!" if dd == 0 else ("내일!" if dd == 1 else f"D-{dd}")
    return f'<span class="dd {cls}">{lab}</span>'


def card(x):
    f = FIELD.get(x["field"], "comm")
    dl = x["deadline"] or ""
    dltxt = f'마감 {dl[5:].replace("-", "/")}' if dl else "마감일 확인"
    s = summ(x["note"])
    link = x["link"] or ""
    a = f'<a class="go" href="{H.escape(link)}" target="_blank" rel="noopener">신청 페이지 ↗</a>' if link else ""
    return f'''<article class="card">
  <div class="ctop"><h3>{H.escape(x["title"])}</h3>{dday_html(x["dday"])}</div>
  <div class="meta"><span class="tag f-{f}">{H.escape(x["field"] or "기타")}</span><span class="st">{H.escape(x["status"])}</span><span class="dl">{dltxt}</span></div>
  {f'<p class="note">{H.escape(s)}</p>' if s else ""}{a}
</article>'''


def build():
    rows, today = fetch()
    imminent = [e for e in rows if isinstance(e["dday"], int) and e["dday"] <= 7]
    byf = {}
    for e in rows:
        byf[e["field"] or "기타"] = byf.get(e["field"] or "기타", 0) + 1
    cards = "\n".join(card(e) for e in rows)

    page = f'''<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><title>반도체 교육·강의 모아보기</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<style>
:root{{--paper:#f5f7f7;--card:#fff;--ink:#161b1a;--muted:#5a6663;--faint:#8b9592;--line:#e0e5e3;--accent:#0e7c86;--accent-soft:#e2f0f0;--proc:#b06f28;--proc-s:#f6ecdd;--digi:#2563a8;--digi-s:#e3edf8;--ana:#7c4dbb;--ana-s:#efe8f8;--comm:#5c6663;--comm-s:#eceeed;--now:#c0453f;--now-s:#f7e2e0;--soon:#b9772a;--soon-s:#f6ecdd;--far:#5a6663;--far-s:#eceeed;--open:#0e7c86;--open-s:#e2f0f0;--mono:ui-monospace,"SF Mono",Menlo,monospace;--sans:system-ui,-apple-system,"Noto Sans KR","Apple SD Gothic Neo",sans-serif;}}
@media(prefers-color-scheme:dark){{:root{{--paper:#0f1413;--card:#161c1b;--ink:#e7ecea;--muted:#98a29f;--faint:#67716e;--line:#252d2b;--accent:#3bb3bd;--accent-soft:#12292c;--proc:#d29a54;--proc-s:#2a2115;--digi:#5b9bd8;--digi-s:#132234;--ana:#a982e0;--ana-s:#241a33;--comm:#98a29f;--comm-s:#232726;--now:#e06b64;--now-s:#2e1917;--soon:#d6a05a;--soon-s:#2a2216;--far:#98a29f;--far-s:#232726;--open:#3bb3bd;--open-s:#12292c;}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:960px;margin:0 auto;padding:48px 22px 80px}}
.eyebrow{{font-family:var(--mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin:0 0 10px}}
h1{{font-size:clamp(26px,5vw,38px);line-height:1.12;margin:0 0 12px;letter-spacing:-.02em;font-weight:700}}
.lede{{font-size:16px;color:var(--muted);margin:0;max-width:60ch}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;flex:1;min-width:120px}}
.stat b{{display:block;font-size:24px;font-family:var(--mono);line-height:1}}.stat span{{font-size:12.5px;color:var(--muted)}}.stat.hot b{{color:var(--now)}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:16px;font-size:12px;color:var(--muted);font-family:var(--mono)}}.legend span{{display:inline-flex;align-items:center;gap:6px}}.dot{{width:10px;height:10px;border-radius:3px}}
h2{{font-size:15px;font-family:var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--faint);margin:40px 0 16px;display:flex;align-items:center;gap:10px}}h2 .n{{color:var(--accent)}}h2::after{{content:"";flex:1;height:1px;background:var(--line)}}
.grid{{display:grid;gap:11px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 17px;transition:border-color .15s}}.card:hover{{border-color:var(--accent)}}
.ctop{{display:flex;align-items:flex-start;gap:12px;justify-content:space-between}}.card h3{{margin:0;font-size:15.5px;font-weight:620;line-height:1.35}}
.dd{{font-family:var(--mono);font-size:12.5px;font-weight:700;padding:3px 10px;border-radius:20px;white-space:nowrap;flex:none}}.dd-now{{background:var(--now-s);color:var(--now)}}.dd-soon{{background:var(--soon-s);color:var(--soon)}}.dd-far{{background:var(--far-s);color:var(--far)}}.dd-open{{background:var(--open-s);color:var(--open)}}
.meta{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}}.tag{{font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:6px}}.f-proc{{background:var(--proc-s);color:var(--proc)}}.f-digi{{background:var(--digi-s);color:var(--digi)}}.f-ana{{background:var(--ana-s);color:var(--ana)}}.f-comm{{background:var(--comm-s);color:var(--comm)}}
.st{{font-size:12px;color:var(--muted)}}.dl{{font-size:12px;color:var(--muted);font-family:var(--mono);margin-left:auto}}.note{{margin:9px 0 0;font-size:13px;color:var(--muted)}}
.go{{display:inline-block;margin-top:11px;font-size:12.5px;font-family:var(--mono);color:var(--accent);text-decoration:none;border:1px solid var(--accent-soft);background:var(--accent-soft);padding:4px 11px;border-radius:6px}}.go:hover{{background:var(--accent);color:#fff}}
footer{{margin-top:50px;padding-top:18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--faint);font-family:var(--mono)}}
@media(max-width:560px){{.ctop{{flex-direction:column;gap:8px}}.dl{{margin-left:0}}}}
</style></head><body>
<div class="wrap">
<p class="eyebrow">2026 여름 · 매일 자동 갱신</p>
<h1>반도체 교육·강의 모아보기</h1>
<p class="lede">전국 대학·기관의 반도체 공정실습·설계교육을 한곳에. 마감 임박순으로 정렬했습니다. 자격·비용은 각 신청 페이지에서 확인하세요.</p>
<div class="stats">
 <div class="stat"><b>{len(rows)}</b><span>전체 교육</span></div>
 <div class="stat hot"><b>{len(imminent)}</b><span>7일 내 마감</span></div>
 <div class="stat"><b>{byf.get("공정",0)}</b><span>공정</span></div>
 <div class="stat"><b>{byf.get("디지털설계",0)+byf.get("아날로그설계",0)}</b><span>설계</span></div>
</div>
<div class="legend">
 <span><i class="dot" style="background:var(--proc)"></i>공정</span>
 <span><i class="dot" style="background:var(--digi)"></i>디지털설계</span>
 <span><i class="dot" style="background:var(--ana)"></i>아날로그설계</span>
 <span><i class="dot" style="background:var(--comm)"></i>공통</span>
 <span><i class="dot" style="background:var(--now)"></i>마감 임박</span>
</div>
<h2>🎓 교육·강의 <span class="n">{len(rows)}</span></h2>
<div class="grid">
{cards}
</div>
<footer>{today} 갱신 · 자동 수집(GitHub Actions) · 마감·자격은 각 신청 페이지에서 최종 확인</footer>
</div></body></html>'''

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"docs/index.html 빌드 완료 — {len(rows)}건 (마감임박 {len(imminent)})")


if __name__ == "__main__":
    build()
