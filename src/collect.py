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


def _build_universe(md: MarketData, store: DataStore, source: str, top: int,
                    limit: int | None) -> list[dict]:
    """수집 대상 유니버스 구성.
    source='master': 종목 마스터(ETF 제외 개별주 전체). source='volume': 거래대금 상위.
    """
    if source == "master":
        # 시가총액(대형주) 우선순위로 수집 → 중요한 종목부터 채움
        rows = store.master_symbols(by_liquidity=True)
        if not rows:
            log("종목 마스터가 비어 있습니다. 먼저 `python -m src.universe` 실행 필요.")
            return []
        if limit:
            rows = rows[:limit]
        return rows
    # volume
    today = datetime.now().strftime("%Y%m%d")
    universe = md.volume_rank(by_value=True, top=top)
    store.save_universe(today, universe)
    return universe


def run(source: str, top: int, months: int, limit: int | None,
        skip_existing: bool, with_extras: bool) -> None:
    cfg = load_config()
    client = KISClient(cfg)
    md = MarketData(client)
    store = DataStore()

    log(f"유니버스 구성 (source={source})")
    universe = _build_universe(md, store, source, top, limit)
    if not universe:
        store.close()
        return
    log(f"  대상 {len(universe)}종목 · 일봉 {months}개월 백필 시작")

    # 스킵 기준: 이미 충분한 일봉이 있으면 건너뜀(재개용)
    min_rows = int(months * 18)  # 월 ~18영업일
    done = 0
    for i, item in enumerate(universe, 1):
        sym, name = item["symbol"], item["name"]
        if skip_existing and len(store.get_daily(sym)) >= min_rows:
            continue
        try:
            n_daily = backfill_daily(md, store, sym, months)
            extra = ""
            if with_extras:
                inv = md.investor_flow(sym)
                store.save_investor(sym, inv)
                fin = md.financial_ratio(sym)
                if fin:
                    store.save_financial(sym, fin)
                extra = f", 투자자 {len(inv)}건"
            done += 1
            ts = datetime.now().strftime("%H:%M:%S")
            store.add_collect_log(ts, "backfill", sym, name,
                                  f"일봉 {n_daily}건{extra} ({i}/{len(universe)})")
            if i % 25 == 0 or n_daily == 0:
                log(f"  [{i}/{len(universe)}] {name}({sym}): 일봉 {n_daily}건{extra}")
        except Exception as e:  # 단일 종목 오류로 전체 백필이 죽지 않도록  # noqa: BLE001
            store.add_collect_log(datetime.now().strftime("%H:%M:%S"), "error", sym, name, str(e))
            log(f"  [{i}/{len(universe)}] {name}({sym}): 오류 {e}")

    log(f"수집 완료 (이번에 {done}종목 처리). 저장 현황:")
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
    ap = argparse.ArgumentParser(description="KIS 학습용 데이터 수집기 (국내)")
    ap.add_argument("--source", choices=["master", "volume"], default="master",
                    help="master=개별주 전체(ETF제외), volume=거래대금 상위")
    ap.add_argument("--top", type=int, default=30, help="[volume] 유니버스 종목 수")
    ap.add_argument("--months", type=int, default=120, help="일봉 백필 개월 수 (기본 10년)")
    ap.add_argument("--limit", type=int, default=None, help="[master] 처리 종목 수 제한")
    ap.add_argument("--skip-existing", action="store_true",
                    help="이미 충분한 일봉이 있는 종목은 건너뜀 (재개용)")
    ap.add_argument("--with-extras", action="store_true",
                    help="투자자동향·재무비율도 함께 수집")
    ap.add_argument("--stats", action="store_true", help="저장 현황만 출력")
    args = ap.parse_args()

    if args.stats:
        show_stats()
    else:
        run(args.source, args.top, args.months, args.limit,
            args.skip_existing, args.with_extras)


if __name__ == "__main__":
    main()
