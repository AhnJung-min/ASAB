"""데이터 수집기 진입점.

거래대금 상위 종목을 유니버스로 잡고, 각 종목의 과거 일봉/투자자동향/재무를
한투 API로 백필하여 SQLite(data/market.db)에 저장한다.

실행:
  python -m src.collect                 # 기본: 상위 30종목, 12개월 일봉
  python -m src.collect --top 50 --months 24
  python -m src.collect --stats         # 저장 현황만 출력
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
from .kis.marketdata import MarketData


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def backfill_daily(md: MarketData, store: DataStore, symbol: str, months: int) -> int:
    """월 단위로 거슬러 올라가며 일봉을 채운다. 한 호출당 ~100영업일."""
    end = datetime.now()
    start_limit = end - timedelta(days=int(months * 31))
    total = 0
    cursor = end
    while cursor > start_limit:
        window_start = max(cursor - timedelta(days=100), start_limit)
        rows = md.daily_candles(
            symbol,
            start=window_start.strftime("%Y%m%d"),
            end=cursor.strftime("%Y%m%d"),
        )
        if not rows:
            break
        total += store.save_daily(symbol, rows)
        # 다음 창은 이번 창의 가장 과거일 하루 전까지
        oldest = datetime.strptime(rows[0]["date"], "%Y%m%d")
        if oldest <= start_limit:
            break
        cursor = oldest - timedelta(days=1)
    return total


def run(top: int, months: int) -> None:
    cfg = load_config()
    client = KISClient(cfg)
    md = MarketData(client)
    store = DataStore()

    today = datetime.now().strftime("%Y%m%d")
    log(f"유니버스 조회: 거래대금 상위 {top}종목")
    universe = md.volume_rank(by_value=True, top=top)
    store.save_universe(today, universe)
    log(f"  유니버스 {len(universe)}종목 저장")

    for i, item in enumerate(universe, 1):
        sym, name = item["symbol"], item["name"]
        try:
            n_daily = backfill_daily(md, store, sym, months)
            inv = md.investor_flow(sym)
            store.save_investor(sym, inv)
            fin = md.financial_ratio(sym)
            if fin:
                store.save_financial(sym, fin)
            log(f"  [{i}/{len(universe)}] {name}({sym}): 일봉 {n_daily}건, 투자자 {len(inv)}건")
        except KISApiError as e:
            log(f"  [{i}/{len(universe)}] {name}({sym}): API 오류 {e}")
        time.sleep(0.1)  # 추가 여유 (클라이언트 스로틀과 별개)

    log("수집 완료. 저장 현황:")
    for k, v in store.stats().items():
        log(f"  {k}: {v:,}")
    store.close()


def show_stats() -> None:
    store = DataStore()
    print("저장 현황 (data/market.db):")
    for k, v in store.stats().items():
        print(f"  {k}: {v:,}")
    store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="KIS 학습용 데이터 수집기")
    ap.add_argument("--top", type=int, default=30, help="유니버스 종목 수 (거래대금 상위)")
    ap.add_argument("--months", type=int, default=12, help="일봉 백필 개월 수")
    ap.add_argument("--stats", action="store_true", help="저장 현황만 출력")
    args = ap.parse_args()

    if args.stats:
        show_stats()
    else:
        run(args.top, args.months)


if __name__ == "__main__":
    main()
