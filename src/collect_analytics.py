"""1군 분석 데이터 수집기 (실전 도메인).

공매도·신용잔고·프로그램매매·투자자매매동향·대차거래를 종목별로 수집한다.
이 엔드포인트들은 모의투자 미지원이라 실전 도메인으로 호출한다.
(주문/금전과 무관한 읽기 전용 시세 조회)

실행:
  python -m src.collect_analytics --months 6 --skip-existing
  python -m src.collect_analytics --limit 100 --months 12
  python -m src.collect_analytics --stats
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore
from .kis.analytics import MarketAnalytics
from .kis.client import KISApiError, KISClient
from .kis.config import load_config


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _page_single(fetch_save, symbol: str, start: datetime, end: datetime,
                 step_days: int = 28, max_empty: int = 2) -> int:
    """단일 날짜 입력 엔드포인트(한 호출 ~30일)를 날짜를 거슬러 페이징.

    fetch_save(date_str) -> 저장 건수. 빈 응답이 max_empty 회 연속이면 중단
    (휴장·일시적 공백 한두 번은 건너뛰고 계속).
    """
    total = 0
    cursor = end
    empty = 0
    while cursor >= start:
        n = fetch_save(_ymd(cursor))
        if n == 0:
            empty += 1
            if empty > max_empty:
                break
        else:
            empty = 0
        total += n
        cursor = cursor - timedelta(days=step_days)
    return total


def _page_range(fetch_save, start: datetime, end: datetime,
                window_days: int = 95) -> int:
    """기간 조회 엔드포인트(한 호출 ~100행 캡)를 윈도우로 잘라 누적.

    fetch_save(start_str, end_str) -> 저장 건수. 공매도 금지기간 등 0 윈도우가
    있어도 start 까지 계속 거슬러 올라간다(윈도우 수 적어 비용 작음).
    """
    total = 0
    chunk_end = end
    while chunk_end >= start:
        chunk_start = max(start, chunk_end - timedelta(days=window_days))
        total += fetch_save(_ymd(chunk_start), _ymd(chunk_end))
        if chunk_start <= start:
            break
        chunk_end = chunk_start - timedelta(days=1)
    return total


def collect_symbol(ana: MarketAnalytics, store: DataStore, symbol: str,
                   start: datetime, end: datetime) -> int:
    """한 종목의 5종 데이터 수집. 개별 호출 오류는 건너뛰고 계속."""
    total = 0

    # 1) 공매도 (기간 조회 — 100행 캡이라 윈도우 페이징)
    try:
        total += _page_range(
            lambda s2, e2: store.save_short_sale(symbol, ana.short_sale(symbol, s2, e2)),
            start, end)
    except KISApiError:
        pass
    # 2) 대차거래 (기간 조회 — 윈도우 페이징)
    try:
        total += _page_range(
            lambda s2, e2: store.save_loan_trans(symbol, ana.loan_trans(symbol, s2, e2)),
            start, end)
    except KISApiError:
        pass
    # 3) 투자자매매동향 (단일일자, 페이징)
    try:
        total += _page_single(
            lambda d: store.save_investor(symbol, ana.investor_daily(symbol, d)),
            symbol, start, end)
    except KISApiError:
        pass
    # 4) 프로그램매매 (단일일자, 페이징)
    try:
        total += _page_single(
            lambda d: store.save_program_trade(symbol, ana.program_trade(symbol, d)),
            symbol, start, end)
    except KISApiError:
        pass
    # 5) 신용잔고 (단일일자, 페이징)
    try:
        total += _page_single(
            lambda d: store.save_credit_balance(symbol, ana.credit_balance(symbol, d)),
            symbol, start, end)
    except KISApiError:
        pass

    return total


def _has_analytics(store: DataStore, symbol: str) -> bool:
    """이미 이 종목의 공매도 데이터가 있으면 수집 완료로 간주(이어받기용)."""
    cur = store.conn.execute(
        "SELECT 1 FROM short_sale WHERE symbol=? LIMIT 1", (symbol,))
    return cur.fetchone() is not None


def run(months: int, limit: int | None, skip_existing: bool) -> None:
    cfg = load_config()
    # 실전 도메인 전용 클라이언트 (같은 키, paper_trading=False)
    real_cfg = dataclasses.replace(cfg, paper_trading=False)
    client = KISClient(real_cfg)
    ana = MarketAnalytics(client)

    end = datetime.now()
    start = end - timedelta(days=int(months * 31))

    with DataStore() as store:
        symbols = [s["symbol"] for s in store.master_symbols(by_liquidity=True)]
        if limit:
            symbols = symbols[:limit]
        log(f"1군 분석 데이터 수집: {len(symbols)}종목 · 최근 {months}개월 "
            f"(실전 도메인)")

        done = 0
        for i, sym in enumerate(symbols, 1):
            if skip_existing and _has_analytics(store, sym):
                continue
            try:
                n = collect_symbol(ana, store, sym, start, end)
                done += 1
                if i % 20 == 0 or n > 0:
                    log(f"  [{i}/{len(symbols)}] {sym}: {n}건")
            except Exception as exc:  # noqa: BLE001
                log(f"  [{i}/{len(symbols)}] {sym}: 오류 {exc}")

        log(f"완료: {done}종목 신규 수집")


def show_stats() -> None:
    with DataStore() as store:
        st = store.stats()
        print("저장 현황 (분석 데이터):")
        for k in ("short_sale", "credit_balance", "program_trade",
                  "loan_trans", "investor_flow"):
            print(f"  {k}: {st.get(k, 0):,}")


def main() -> None:
    ap = argparse.ArgumentParser(description="1군 분석 데이터 수집 (실전 도메인)")
    ap.add_argument("--months", type=int, default=6, help="수집 기간(개월), 기본 6")
    ap.add_argument("--limit", type=int, default=None, help="처리 종목 수 제한")
    ap.add_argument("--skip-existing", action="store_true",
                    help="이미 수집된 종목 건너뛰기")
    ap.add_argument("--stats", action="store_true", help="저장 현황만 출력")
    args = ap.parse_args()

    if args.stats:
        show_stats()
        return
    run(args.months, args.limit, args.skip_existing)


if __name__ == "__main__":
    main()
