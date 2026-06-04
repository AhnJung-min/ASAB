"""횡단면 팩터 로테이션 백테스트.

매 리밸런스(기본 20영업일)마다, 그 시점까지의 데이터만으로 팩터 점수를 계산해
상위 N종목을 동일비중 보유한다. 다음 리밸런스에 교체한다.

미래정보 누출(lookahead) 방지:
  - t시점 결정은 t시점까지의 종가만 사용
  - 수익은 t -> t+h 의 미래 종가로 실현 (결정 이후 구간)
종가-종가 기준이며, 거래비용은 회전율에 bps로 부과한다.
"""
from __future__ import annotations

import argparse
import bisect
import math
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore
from .screener import assign_scores, compute_factors

BENCHMARK_SYMBOL = "069500"  # KODEX 200 (있으면 벤치마크로 사용)


def _load_series(store: DataStore) -> dict[str, list[dict]]:
    """symbol -> [{date, close, value}, ...] (과거→최근)."""
    series: dict[str, list[dict]] = {}
    for sym in store.symbols():
        rows = store.get_daily(sym)
        series[sym] = [
            {"date": r["date"], "close": r["close"], "value": r["value"]} for r in rows
        ]
    return series


def _trading_dates(series: dict[str, list[dict]]) -> list[str]:
    dates: set[str] = set()
    for rows in series.values():
        dates.update(r["date"] for r in rows)
    return sorted(dates)


def run_backtest(
    store: DataStore,
    top_n: int = 3,
    hold_days: int = 20,
    cost_bps: float = 25.0,
    require_financials: bool = True,
    regime_filter: bool = False,
    regime_ma: int = 200,
    start_date: str | None = None,
    end_date: str | None = None,
    max_pool: int | None = None,
    series: dict[str, list[dict]] | None = None,
) -> dict[str, Any]:
    # series 를 외부에서 주입하면 재로딩 생략(워크포워드에서 반복 호출 시 성능)
    if series is None:
        series = _load_series(store)
    if not series:
        return {"error": "데이터가 없습니다. 먼저 python -m src.collect 를 실행하세요."}

    # 개별주 유니버스(종목 마스터, ETF/ETN 제외). 없으면 재무보유종목으로 폴백.
    master_rows = store.master_symbols()
    master_set = {r["symbol"] for r in master_rows}
    liq_of = {r["symbol"]: (r.get("liquidity") or 0) for r in master_rows}
    if not master_set:
        master_set = {
            r["symbol"]
            for r in store.conn.execute("SELECT DISTINCT symbol FROM financial_ratio")
        }
    pool = [
        s for s in series
        if (not require_financials or s in master_set) and len(series[s]) >= 61
    ]
    # 유동성(시총) 상위로 제한 → 의미있는 유니버스 + 속도
    if max_pool and len(pool) > max_pool:
        pool.sort(key=lambda s: liq_of.get(s, 0), reverse=True)
        pool = pool[:max_pool]
    if len(pool) < top_n:
        return {
            "error": f"백테스트 대상 종목이 부족합니다(개별주 {len(pool)}개 < top_n {top_n}). "
            "유니버스를 늘리거나 --include-etf 를 사용하세요."
        }

    dates = _trading_dates({s: series[s] for s in pool})
    # 종목별 날짜->종가 빠른 조회 (벤치마크는 pool 밖이어도 포함)
    close_lookup_syms = set(pool)
    if BENCHMARK_SYMBOL in series:
        close_lookup_syms.add(BENCHMARK_SYMBOL)
    close_by: dict[str, dict[str, int]] = {
        s: {r["date"]: r["close"] for r in series[s]} for s in close_lookup_syms
    }
    # 종목별 (날짜 -> 인덱스) : as-of 슬라이싱용
    idx_by: dict[str, dict[str, int]] = {
        s: {r["date"]: i for i, r in enumerate(series[s])} for s in pool
    }

    # --- 벤치마크(지수) 배열: 레짐 필터 + 상대강도에 공통 사용 ---
    has_bench = BENCHMARK_SYMBOL in series
    bdates = [r["date"] for r in series[BENCHMARK_SYMBOL]] if has_bench else []
    bcloses = [r["close"] for r in series[BENCHMARK_SYMBOL]] if has_bench else []

    def _bench_pos(t: str) -> int:
        return bisect.bisect_right(bdates, t) - 1  # t 이하 마지막 지수 거래일

    def index_mom_asof(t: str) -> float | None:
        """t시점 지수 모멘텀(직전5일 제외 60일). 미래정보 누출 없음."""
        if not has_bench:
            return None
        pos = _bench_pos(t)
        if pos < 60:
            return None
        return bcloses[pos - 5] / bcloses[pos - 60] - 1

    # 시장국면(레짐): 지수가 N일선 위면 risk-on(투자). t 이하 종가만 사용.
    regime_on: dict[str, bool] = {}
    has_regime = regime_filter and has_bench and len(bcloses) >= regime_ma
    if has_regime:
        for t in dates:
            pos = _bench_pos(t)
            if pos >= regime_ma - 1:
                sma = sum(bcloses[pos - regime_ma + 1: pos + 1]) / regime_ma
                regime_on[t] = bcloses[pos] > sma
            else:
                regime_on[t] = True  # 워밍업 구간은 투자 허용

    def is_risk_on(t: str) -> bool:
        return regime_on.get(t, True) if has_regime else True

    cost = cost_bps / 10_000.0
    in_market_days = 0
    span_days = 0
    equity = 1.0
    bench_equity = 1.0
    curve: list[dict] = []
    holdings_log: list[dict] = []

    # 워밍업(60) 또는 start_date 중 늦은 쪽에서 매매 시작
    start_i = 60
    if start_date:
        start_i = max(start_i, bisect.bisect_left(dates, start_date))
    prev_holdings: list[str] = []

    i = start_i
    while i < len(dates):
        t = dates[i]
        if end_date and t > end_date:
            break

        # --- t시점 후보 점수 계산 (t까지만 사용) ---
        idx_mom_t = index_mom_asof(t)  # 상대강도 기준(지수 모멘텀)
        candidates = []
        for s in pool:
            if t not in idx_by[s]:
                continue
            end_idx = idx_by[s][t]
            if end_idx < 60:
                continue
            rows_asof = series[s][: end_idx + 1]
            f = compute_factors(rows_asof)
            if f is None:
                continue
            f["symbol"] = s
            f["rel_strength"] = (f["momentum"] - idx_mom_t) if idx_mom_t is not None else f["momentum"]
            candidates.append(f)

        ranked = assign_scores(candidates)
        picks = [c["symbol"] for c in ranked[:top_n]]

        # 시장국면 필터: risk-off 면 현금 보유(picks 비움)
        if not is_risk_on(t):
            picks = []

        # 회전율 기반 거래비용 (교체된 비중만큼, 0~1 정규화)
        denom = max(len(picks), len(prev_holdings), 1)
        turnover = len(set(picks) ^ set(prev_holdings)) / denom
        equity *= (1 - cost * turnover)
        prev_holdings = picks

        holdings_log.append({"date": t, "picks": picks,
                             "names": [store.name_of(p) for p in picks]})

        # --- 다음 리밸런스까지 보유, 일별 마크투마켓 ---
        j_end = min(i + hold_days, len(dates) - 1)
        # 진입 종가
        entry = {s: close_by[s].get(t) for s in picks}
        prev_port = equity
        prev_bench = bench_equity
        last_close = dict(entry)
        bench_entry = close_by.get(BENCHMARK_SYMBOL, {}).get(t) if BENCHMARK_SYMBOL in series else None
        bench_last = bench_entry

        for k in range(i + 1, j_end + 1):
            d = dates[k]
            # 전략: 동일비중 종목들의 일별 수익률 평균
            rets = []
            for s in picks:
                c0 = last_close.get(s)
                c1 = close_by[s].get(d)
                if c0 and c1:
                    rets.append(c1 / c0 - 1)
                    last_close[s] = c1
            if rets:
                prev_port *= (1 + sum(rets) / len(rets))
            # 벤치마크
            if bench_last:
                bc = close_by.get(BENCHMARK_SYMBOL, {}).get(d)
                if bc:
                    prev_bench *= (bc / bench_last)
                    bench_last = bc
            curve.append({"date": d, "strategy": prev_port, "benchmark": prev_bench})

        window = j_end - i
        span_days += window
        if picks:
            in_market_days += window

        equity = prev_port
        bench_equity = prev_bench
        i = j_end if j_end > i else i + 1

    metrics = _metrics(curve)
    metrics["time_in_market"] = (in_market_days / span_days) if span_days else 1.0
    return {
        "curve": curve,
        "metrics": metrics,
        "holdings_log": holdings_log,
        "params": {
            "top_n": top_n, "hold_days": hold_days, "cost_bps": cost_bps,
            "regime_filter": regime_filter, "regime_ma": regime_ma,
        },
        "pool_size": len(pool),
        "has_benchmark": BENCHMARK_SYMBOL in series,
        "regime_applied": has_regime,
    }


def _metrics(curve: list[dict]) -> dict[str, float]:
    if len(curve) < 2:
        return {}
    eq = [p["strategy"] for p in curve]
    bench = [p["benchmark"] for p in curve]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]

    total = eq[-1] - 1
    days = len(eq)
    years = days / 252
    cagr = eq[-1] ** (1 / years) - 1 if years > 0 else 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0.0

    peak = eq[0]
    mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)

    bench_total = bench[-1] - 1
    return {
        "total_return": total,
        "bench_return": bench_total,
        "cagr": cagr,
        "sharpe": sharpe,
        "mdd": mdd,
        "volatility": std * math.sqrt(252),
        "trading_days": days,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="팩터 로테이션 백테스트")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--hold-days", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=25.0)
    ap.add_argument("--include-etf", action="store_true")
    ap.add_argument("--regime-filter", action="store_true",
                    help="지수 이동평균 아래면 현금 보유(시장국면 필터)")
    ap.add_argument("--regime-ma", type=int, default=200, help="국면 판정 이동평균 일수")
    args = ap.parse_args()

    store = DataStore()
    res = run_backtest(
        store, top_n=args.top_n, hold_days=args.hold_days,
        cost_bps=args.cost_bps, require_financials=not args.include_etf,
        regime_filter=args.regime_filter, regime_ma=args.regime_ma,
    )
    store.close()

    if "error" in res:
        print(res["error"])
        return
    m = res["metrics"]
    rf = "ON" if res["params"]["regime_filter"] else "OFF"
    print(f"대상 종목 풀: {res['pool_size']}개 / 국면필터 {rf}(적용={res['regime_applied']})")
    print(f"누적수익률 : {m['total_return']*100:+.1f}%  (벤치마크 {m['bench_return']*100:+.1f}%)")
    print(f"CAGR       : {m['cagr']*100:+.1f}%")
    print(f"샤프지수   : {m['sharpe']:.2f}")
    print(f"최대낙폭   : {m['mdd']*100:.1f}%")
    print(f"연변동성   : {m['volatility']*100:.1f}%")
    print(f"투자기간비율: {m['time_in_market']*100:.0f}%  (나머지는 현금)")


if __name__ == "__main__":
    main()
