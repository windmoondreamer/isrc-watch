"""SOXL/SOXS 페이퍼 트레이딩 엔진 (재미예산 80만원, 지침 v1).

매일 미국장 마감 후(KST 아침 07:10) 실행. 방금 끝난 세션을 지침 v1 규칙으로
시뮬레이션해서 💸 트레이딩 로그 DB에 기록하고, 다음 세션 계획을 함께 남긴다.
실제 주문은 내지 않는다 — KIS 실전 전환 전의 검증 관문(모의 20거래일).

지침 v1 규칙 (노션 "SOXL/SOXS 트레이딩 지침" 참조):
  진입: SOXL 전일 종가 > SMA20, 전일 변동폭 ±8% 이내, 이벤트일 아님 → 시가 매수
  청산: -5% 손절 / +10% 익절 / 미도달 시 종가 청산 (오버나이트 금지)
  서킷브레이커: 주간 누적 -15% → 그 주 중단 / 월간 -30% → 그 달 중단
  SOXS: 자동 엔진에서는 항상 제외 (지침상 재량 예외라 기계 판정 불가)

손절/익절 체결은 당일 고가·저가로 근사하며, 둘 다 걸린 날은 손절 우선(보수적).
"""

import sys
from datetime import date, datetime, timezone

from notion import KST, TRADING_DB, WEEKDAY_KR, api, http

FEE_RT = 0.001          # 왕복 수수료+스프레드 (0.05%/편도 가정)
SL, TP = 0.05, 0.10     # 손절 -5% / 익절 +10%
SMA_LEN = 20
PREV_MOVE_CAP = 0.08
START_EQUITY = 580.0    # USD, 약 80만원
WEEK_BREAKER = -0.15
MONTH_BREAKER = -0.30

# 알려진 대형 이벤트일(미국 거래일 기준) — 발견 시 여기에 추가하면 그날 관망.
# 자동 수집 아님(실적 캘린더 API가 인증을 요구해서). 브리핑에서 본 날짜를 수동 등록.
EVENT_DATES = {
    # "2026-07-28",  # 예: FOMC
}

CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         "{sym}?range={rng}&interval={iv}")


def bars(sym):
    res = http(CHART.format(sym=sym, rng="6mo", iv="1d"))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c) or o <= 0:
            continue
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        out.append({"d": d, "o": o, "h": h, "l": l, "c": c})

    # 야후가 방금 끝난 세션의 일봉을 불완전하게(close=None) 주는 경우가 있어
    # 당일 5분봉으로 재구성해 보강한다 (2026-07-14 실제 발생).
    res5 = http(CHART.format(sym=sym, rng="1d", iv="5m"))["chart"]["result"][0]
    q5 = res5["indicators"]["quote"][0]
    d5 = datetime.fromtimestamp(res5["timestamp"][0], timezone.utc).date()
    if out and d5 > out[-1]["d"]:
        opens = [x for x in q5["open"] if x]
        highs = [x for x in q5["high"] if x]
        lows = [x for x in q5["low"] if x]
        closes = [x for x in q5["close"] if x]
        if opens and closes:
            out.append({"d": d5, "o": opens[0], "h": max(highs),
                        "l": min(lows), "c": closes[-1]})
    return out


def decide(closes, prev_bar, session_day):
    """지침 v1 진입 체크리스트. (추천, 체크리스트 문자열) 반환."""
    sma = sum(closes[-SMA_LEN:]) / SMA_LEN
    above = closes[-1] > sma
    prev_move = abs(prev_bar["c"] / prev_bar["o"] - 1)
    calm = prev_move <= PREV_MOVE_CAP
    no_event = session_day.isoformat() not in EVENT_DATES
    checks = [
        f"{'✅' if above else '❌'} 종가 ${closes[-1]:.2f} vs SMA20 ${sma:.2f}",
        f"{'✅' if calm else '❌'} 전일 변동폭 {prev_move * 100:.1f}% (기준 ±8%)",
        f"{'✅' if no_event else '❌'} 등록된 이벤트일 아님",
    ]
    go = above and calm and no_event
    return ("SOXL" if go else "관망"), " / ".join(checks)


def simulate(bar):
    """시가 진입 → 밴드/종가 청산. (결과, 손익률) 반환."""
    o = bar["o"]
    if bar["l"] <= o * (1 - SL):
        return "손절", -SL - FEE_RT
    if bar["h"] >= o * (1 + TP):
        return "익절", TP - FEE_RT
    return "종가청산", bar["c"] / o - 1 - FEE_RT


def fetch_log():
    """로그 DB 전체를 거래일 오름차순으로."""
    rows, cursor = [], None
    while True:
        payload = {"page_size": 100,
                   "sorts": [{"property": "거래일", "direction": "ascending"}]}
        if cursor:
            payload["start_cursor"] = cursor
        res = api(f"/databases/{TRADING_DB}/query", "POST", payload)
        rows.extend(res["results"])
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    out = []
    for r in rows:
        p = r["properties"]
        d = p["거래일"].get("date")
        out.append({
            "d": date.fromisoformat(d["start"]) if d else None,
            "ret": p["손익률"].get("number"),
            "equity": p["누적자산"].get("number"),
        })
    return [r for r in out if r["d"]]


def breaker(log, session_day):
    """주간/월간 서킷브레이커 발동 여부."""
    def cum(rows):
        eq = 1.0
        for r in rows:
            if r["ret"] is not None:
                eq *= 1 + r["ret"]
        return eq - 1
    iso = session_day.isocalendar()[:2]
    week = [r for r in log if r["d"].isocalendar()[:2] == iso]
    month = [r for r in log if (r["d"].year, r["d"].month)
             == (session_day.year, session_day.month)]
    if cum(month) <= MONTH_BREAKER:
        return f"월간 서킷브레이커 ({cum(month) * 100:.1f}% ≤ -30%)"
    if cum(week) <= WEEK_BREAKER:
        return f"주간 서킷브레이커 ({cum(week) * 100:.1f}% ≤ -15%)"
    return None


def write_row(session_day, rec, result, entry, ret, equity, checklist, dry):
    title = f"{session_day.isoformat()} ({WEEKDAY_KR[session_day.weekday()]})"
    if dry:
        print(f"[dry-run] {title} | {rec} | {result} | ret={ret} | eq={equity:.2f}")
        print(f"[dry-run] {checklist}")
        return
    props = {
        "날짜": {"title": [{"text": {"content": title}}]},
        "거래일": {"date": {"start": session_day.isoformat()}},
        "추천": {"select": {"name": rec}},
        "체크리스트": {"rich_text": [{"text": {"content": checklist[:1900]}}]},
        "누적자산": {"number": round(equity, 2)},
    }
    if result:
        props["결과"] = {"select": {"name": result}}
    if entry:
        props["진입가"] = {"number": round(entry, 2)}
        props["손절가"] = {"number": round(entry * (1 - SL), 2)}
        props["익절가"] = {"number": round(entry * (1 + TP), 2)}
    if ret is not None:
        props["손익률"] = {"number": round(ret, 4)}
    api("/pages", "POST", {"parent": {"database_id": TRADING_DB},
                           "properties": props})
    print(f"기록됨: {title} | {rec} | {result} | eq=${equity:.2f}")


BACKFILL_LIMIT = 5  # 미기록 세션 소급 처리 상한 (Actions 장애 대비)


def main():
    dry = "--dry-run" in sys.argv
    soxl = bars("SOXL")
    now = datetime.now(timezone.utc)
    today_utc = now.date()

    log = [] if dry else fetch_log()
    equity = next((r["equity"] for r in reversed(log)
                   if r["equity"] is not None), START_EQUITY)
    logged = {r["d"] for r in log}

    # 완료됐는데 미기록인 세션들 (최근 BACKFILL_LIMIT개, 오래된 것부터).
    # 오늘 세션은 마감 이후(21:00 UTC~)에만 완료로 간주.
    pending = [i for i, b in enumerate(soxl[-BACKFILL_LIMIT - 1:], len(soxl) - min(BACKFILL_LIMIT + 1, len(soxl)))
               if b["d"] not in logged and i >= SMA_LEN + 1
               and (b["d"] < today_utc or now.hour >= 21)]

    if not pending:
        if (now.hour >= 21 and today_utc.weekday() < 5
                and soxl[-1]["d"] != today_utc and today_utc not in logged):
            write_row(today_utc, "휴장", "휴장", None, None, equity,
                      f"미국 휴장 (마지막 거래일 {soxl[-1]['d']})", dry)
        else:
            print(f"기록할 세션 없음 (마지막 완료 세션 {soxl[-1]['d']})")
        return

    for i in pending:
        bar = soxl[i]
        hist = soxl[:i]                   # 그 세션 개장 전 데이터
        closes = [b["c"] for b in hist]
        br = breaker(log, bar["d"])
        if br:
            rec, checklist = "중단", br
            result, entry, ret = "중단", None, None
        else:
            rec, checklist = decide(closes, hist[-1], bar["d"])
            if rec == "SOXL":
                entry = bar["o"]
                result, ret = simulate(bar)
                equity *= 1 + ret
            else:
                result, entry, ret = "관망", None, None

        if i == pending[-1]:
            # 다음 세션 계획 (이 세션 종가까지 반영) + 통산 성적
            next_rec, next_checks = decide(
                [b["c"] for b in soxl[:i + 1]], bar, date.max)
            checklist += f"  ||  다음 세션 계획: {next_rec} ({next_checks})"
            trades = [r for r in log if r["ret"] is not None]
            if trades or ret is not None:
                n = len(trades) + (ret is not None)
                wins = (sum(1 for t in trades if t["ret"] > 0)
                        + (1 if ret is not None and ret > 0 else 0))
                checklist += (f"  ||  통산 {n}거래 승률 {wins / n * 100:.0f}% "
                              f"누적 {(equity / START_EQUITY - 1) * 100:+.1f}%")

        write_row(bar["d"], rec, result, entry if rec == "SOXL" else None,
                  ret, equity, checklist, dry)
        log.append({"d": bar["d"], "ret": ret, "equity": equity})


if __name__ == "__main__":
    main()
