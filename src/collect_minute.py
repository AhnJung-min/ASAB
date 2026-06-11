"""EOD 분봉 수집 — 그날 급등주 스캔에 등장한 종목들의 당일 1분봉을 저장한다.

목적: 학습 라벨의 생존편향 제거. 스캔 forward-return은 'top-40에 남은' 종목만
가격을 알 수 있어(이탈=대개 하락→라벨이 승자 쪽으로 편향) 모델이 낙관적이 된다.
분봉이 있으면 이탈 종목의 실제 N분 뒤 가격도 알 수 있어 편향이 사라진다.

⚠️ KIS '당일분봉'은 오늘 데이터만 준다(과거 백필 불가). 반드시 **장 마감 후
같은 날**(15:30~자정) 실행할 것. 한 번에 ~30봉이라 하루치는 페이징한다.

  python -m src.collect_minute            # 오늘 스캔 종목의 당일 분봉 수집
  python -m src.collect_minute --limit 50 # 상위 N종목만(테스트)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore
from .kis.client import KISApiError, KISClient
from .kis.config import load_config
from .kis.domestic import DomesticStock
from .kis.marketdata import MarketData

_OPEN = "090000"
_CLOSE = "153000"
_MAX_PAGES = 20   # 안전상한(하루 390분 / 30봉 ≈ 13페이지)
_DAILY_BACKFILL_MONTHS = 2   # 일봉 보충 깊이(RVOL 분모는 20일 평균이면 충분)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _dec_minute(hhmmss: str) -> str:
    """HHMMSS 에서 1분 뺀 HHMMSS(페이징 커서)."""
    t = datetime.strptime(hhmmss, "%H%M%S")
    return (t.replace(second=0) - timedelta(minutes=1)).strftime("%H%M%S")


def fetch_day(dom: DomesticStock, symbol: str) -> list[dict]:
    """당일 분봉 전체를 페이징해 수집(시간순)."""
    bars: dict[str, dict] = {}
    cursor = _CLOSE
    for _ in range(_MAX_PAGES):
        try:
            chunk = dom.minute_bars(symbol, cursor)
        except KISApiError:
            break
        if not chunk:
            break
        new = 0
        for b in chunk:
            if b["time"] not in bars:
                bars[b["time"]] = b
                new += 1
        earliest = min(b["time"] for b in chunk)
        if earliest <= _OPEN or new == 0:
            break
        cursor = _dec_minute(earliest)
    return sorted(bars.values(), key=lambda b: b["time"])


def backfill_missing_daily(client: KISClient, store: DataStore, date: str) -> None:
    """당일 스캔에 등장했지만 일봉이 없는(또는 오래된) 개별주의 일봉을 짧게 백필.

    목적: 단타봇 RVOL 피처의 분모(20일 평균거래량)가 daily_price 기반이라,
    일봉이 없는 신규상장·신형코드 종목은 rvol=NULL 로 기록됨(스캔의 ~28%).
    ETF/레버리지는 단타 매수 대상이 아니므로 건너뛴다(호출량 절약).
    """
    from .collect import backfill_daily
    from .surge_bot import is_etf, is_leverage_inverse

    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    rows = store.conn.execute(
        "SELECT DISTINCT symbol, name FROM surge_scan WHERE substr(ts,1,10)=?",
        (date,)).fetchall()
    todo = []
    for r in rows:
        if is_etf(r["name"]) or is_leverage_inverse(r["name"]):
            continue
        ld = store.latest_date(r["symbol"])
        if ld is None or ld < cutoff:   # 일봉 없음 또는 1주 이상 공백
            todo.append((r["symbol"], r["name"]))
    if not todo:
        log("일봉 보충: 대상 없음(스캔 종목 전부 최신).")
        return

    log(f"일봉 보충 시작: {len(todo)}종목 (RVOL 결손 방지, {_DAILY_BACKFILL_MONTHS}개월)")
    md = MarketData(client)
    n_ok = 0
    for i, (sym, name) in enumerate(todo, 1):
        try:
            n = backfill_daily(md, store, sym, _DAILY_BACKFILL_MONTHS)
            if n > 0:
                n_ok += 1
            if i % 20 == 0 or i == len(todo):
                log(f"  [{i}/{len(todo)}] 일봉 채움 {n_ok}종목")
        except Exception as e:  # 한 종목 오류로 전체가 죽지 않게  # noqa: BLE001
            log(f"  일봉 보충 오류 {name}({sym}): {e}")
    log(f"일봉 보충 완료: {n_ok}/{len(todo)}종목.")


def main() -> None:
    ap = argparse.ArgumentParser(description="EOD 분봉 수집(급등주 스캔 종목)")
    ap.add_argument("--limit", type=int, default=0, help="대상 종목 수 제한(0=전체)")
    ap.add_argument("--date", default=None, help="대상일 YYYY-MM-DD(기본 오늘)")
    args = ap.parse_args()

    store = DataStore()
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    yyyymmdd = date.replace("-", "")
    symbols = store.scan_symbols_on(date)
    done = store.minute_symbols_done(yyyymmdd)
    todo = [s for s in symbols if s not in done]
    if args.limit:
        todo = todo[: args.limit]

    log(f"분봉 수집 대상일 {date} · 스캔종목 {len(symbols)} · "
        f"이미수집 {len(done)} · 이번 {len(todo)}")
    client = KISClient(load_config())
    if todo:
        dom = DomesticStock(client)
        total_bars = 0
        for i, sym in enumerate(todo, 1):
            bars = fetch_day(dom, sym)
            if bars:
                total_bars += store.save_minute_bars(sym, bars)
            if i % 20 == 0 or i == len(todo):
                log(f"  [{i}/{len(todo)}] 누적 {total_bars:,}봉")
        log(f"완료. {len(todo)}종목 · {total_bars:,}봉 저장.")
    else:
        log("수집할 분봉이 없습니다(스캔 데이터 없음 또는 이미 완료).")

    # 일봉이 빠진 스캔 종목 보충(RVOL 결손 방지) — 분봉과 같은 EOD 타이밍에 1회
    if symbols:
        backfill_missing_daily(client, store, date)
    store.close()


if __name__ == "__main__":
    main()
