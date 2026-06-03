"""종목 스크리너. 저장된 일봉 데이터로 팩터를 계산해 매매 후보를 순위화한다.

팩터(횡단면 비교):
  - momentum : 최근 60영업일 수익률(직전 5일 제외 = 단기 반전 노이즈 제거)
  - trend    : 종가가 20일 이동평균 위에 있는지
  - liquidity: 최근 20일 평균 거래대금 (클수록 좋음)
  - low_vol  : 일간 수익률 변동성 (작을수록 좋음)

각 팩터를 백분위 순위로 환산해 가중 합산 -> 종합점수.
ETF/ETN 등 재무비율이 없는 종목은 기본적으로 제외(개별주 위주).
"""
from __future__ import annotations

import argparse
import statistics
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore

# 팩터 가중치 (합 1.0)
WEIGHTS = {"momentum": 0.4, "trend": 0.2, "liquidity": 0.2, "low_vol": 0.2}
MIN_HISTORY = 60          # 최소 일봉 개수
MIN_AVG_VALUE = 1_000_000_000  # 최소 평균 거래대금(원). 유동성 필터


def _returns(closes: list[int]) -> list[float]:
    return [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]


def compute_factors(rows) -> dict | None:
    if len(rows) < MIN_HISTORY:
        return None
    closes = [r["close"] for r in rows]
    values = [r["value"] for r in rows]

    avg_value = statistics.mean(values[-20:])
    if avg_value < MIN_AVG_VALUE:
        return None

    # 모멘텀: 직전 5일 제외한 60일 수익률
    momentum = closes[-6] / closes[-61] - 1 if len(closes) >= 61 else closes[-1] / closes[0] - 1
    ma20 = statistics.mean(closes[-20:])
    trend = 1.0 if closes[-1] > ma20 else 0.0
    rets = _returns(closes[-21:])
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0

    return {
        "momentum": momentum,
        "trend": trend,
        "liquidity": avg_value,
        "vol": vol,
        "last_close": closes[-1],
    }


def _percentile_rank(values: list[float]) -> list[float]:
    """값들을 0~1 백분위로. 동률은 평균 순위."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    for pos, idx in enumerate(order):
        ranks[idx] = pos / (n - 1)
    return ranks


def assign_scores(candidates: list[dict]) -> list[dict]:
    """팩터 dict 리스트를 횡단면 백분위로 환산해 score 부여 후 내림차순 정렬.

    백테스트에서 '특정 시점의 후보들'에도 그대로 재사용한다.
    """
    if not candidates:
        return []
    mom = _percentile_rank([c["momentum"] for c in candidates])
    liq = _percentile_rank([c["liquidity"] for c in candidates])
    # 변동성은 낮을수록 좋으므로 부호 반전
    lowvol = _percentile_rank([-c["vol"] for c in candidates])

    for i, c in enumerate(candidates):
        c["score"] = (
            WEIGHTS["momentum"] * mom[i]
            + WEIGHTS["trend"] * c["trend"]
            + WEIGHTS["liquidity"] * liq[i]
            + WEIGHTS["low_vol"] * lowvol[i]
        )
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def screen(store: DataStore, require_financials: bool = True) -> list[dict]:
    candidates = []
    fin_symbols = {
        r["symbol"]
        for r in store.conn.execute("SELECT DISTINCT symbol FROM financial_ratio")
    }
    for sym in store.symbols():
        if require_financials and sym not in fin_symbols:
            continue  # ETF/ETN 등 제외
        f = compute_factors(store.get_daily(sym))
        if f is None:
            continue
        f["symbol"] = sym
        f["name"] = store.name_of(sym)
        candidates.append(f)

    return assign_scores(candidates)


def main() -> None:
    ap = argparse.ArgumentParser(description="종목 스크리너")
    ap.add_argument("--top", type=int, default=10, help="상위 N종목 출력")
    ap.add_argument("--include-etf", action="store_true", help="재무 없는 종목(ETF 등)도 포함")
    args = ap.parse_args()

    store = DataStore()
    ranked = screen(store, require_financials=not args.include_etf)
    if not ranked:
        print("후보가 없습니다. 먼저 `python -m src.collect` 로 데이터를 수집하세요.")
        store.close()
        return

    print(f"{'순위':>2} {'종목명':<16} {'코드':<8} {'점수':>5} {'모멘텀':>7} {'추세':>4} {'거래대금(억)':>11}")
    print("-" * 70)
    for i, c in enumerate(ranked[: args.top], 1):
        print(
            f"{i:>2} {c['name']:<16} {c['symbol']:<8} "
            f"{c['score']:>5.2f} {c['momentum']*100:>6.1f}% "
            f"{'↑' if c['trend'] else '↓':>4} {c['liquidity']/1e8:>10,.0f}"
        )
    store.close()


if __name__ == "__main__":
    main()
