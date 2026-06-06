"""타깃 포트폴리오 산출 — 연구(스크리너/국면)와 실거래의 다리.

백테스트(`backtest.py`)에서 검증한 것과 동일한 의사결정을 라이브에 적용한다:
  1) 스크리너 팩터 점수로 상위 N종목 선정 (횡단면 로테이션)
  2) 시장국면(레짐) 필터: 지수가 N일선 아래면 현금 보유(타깃 비움)

이렇게 해야 "검증된 전략"이 그대로 실거래로 흘러간다. run_bot 은 이 타깃을
받아 계좌를 리밸런싱한다. DB만 읽으므로 KIS API 를 호출하지 않는다.
"""
from __future__ import annotations

from typing import Any

from .data.store import DataStore
from .screener import BENCHMARK_SYMBOL, screen


def market_regime(store: DataStore, regime_ma: int = 200) -> tuple[bool, str | None]:
    """지수(KODEX200)가 N일 이동평균 위면 risk-on(True). (on, 기준일) 반환.

    데이터가 부족하면 보수적으로 투자 허용(True)하되 기준일은 None.
    """
    rows = store.get_daily(BENCHMARK_SYMBOL)
    if len(rows) < regime_ma:
        return True, (rows[-1]["date"] if rows else None)
    closes = [r["close"] for r in rows]
    sma = sum(closes[-regime_ma:]) / regime_ma
    return closes[-1] > sma, rows[-1]["date"]


def target_portfolio(store: DataStore, top_n: int = 5, regime_filter: bool = True,
                     regime_ma: int = 200) -> dict[str, Any]:
    """상위 N 타깃 종목 + 국면 정보 반환.

    반환: {
      "asof": 기준 데이터 날짜,
      "regime_on": bool,
      "picks": [{symbol,name,rank,score}, ...]  # 국면 off 면 빈 리스트(현금)
    }
    """
    ranked = screen(store)  # 개별주(ETF 제외) 팩터 순위, 유동성 필터 포함
    on, asof = market_regime(store, regime_ma) if regime_filter else (True, None)
    if asof is None and ranked:
        asof = store.latest_date(ranked[0]["symbol"])

    picks: list[dict[str, Any]] = []
    if on or not regime_filter:
        for i, c in enumerate(ranked[:top_n], 1):
            picks.append({
                "symbol": c["symbol"],
                "name": c.get("name") or store.name_of(c["symbol"]),
                "rank": i,
                "score": round(c.get("score", 0.0), 4),
                "last_close": c.get("last_close"),
            })
    return {"asof": asof, "regime_on": on, "picks": picks}


def main() -> None:
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="타깃 포트폴리오 미리보기(주문 없음)")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--no-regime", action="store_true", help="시장국면 필터 끄기")
    ap.add_argument("--regime-ma", type=int, default=200)
    args = ap.parse_args()

    store = DataStore()
    tp = target_portfolio(store, top_n=args.top_n,
                          regime_filter=not args.no_regime, regime_ma=args.regime_ma)
    store.close()

    state = "risk-ON(투자)" if tp["regime_on"] else "risk-OFF(현금)"
    print(f"기준일 {tp['asof']} · 시장국면 {state}")
    if not tp["picks"]:
        print("타깃 없음(현금 보유).")
        return
    print(f"{'순위':>2} {'종목명':<16} {'코드':<8} {'점수':>6} {'종가':>9}")
    print("-" * 48)
    for p in tp["picks"]:
        print(f"{p['rank']:>2} {p['name']:<16} {p['symbol']:<8} "
              f"{p['score']:>6.3f} {(p['last_close'] or 0):>9,}")


if __name__ == "__main__":
    main()
