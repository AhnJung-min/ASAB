"""횡단면 팩터 로테이션 백테스트.

매 리밸런스(기본 20영업일)마다, 그 시점까지의 데이터만으로 팩터 점수를 계산해
상위 N종목을 동일비중 보유한다. 다음 리밸런스에 교체한다.

미래정보 누출(lookahead) 방지:
  - t시점 결정은 t시점까지의 종가만 사용
  - 수익은 t -> t+h 의 미래 종가로 실현 (결정 이후 구간)
종가-종가 기준이며, 거래비용은 회전율에 bps로 부과한다.

거래비용 = ① 수수료·세금(cost_bps) + ② 슬리피지(slippage_bps, 유동성 차등).
  슬리피지는 체결가가 밀리는 비용으로 수수료와 별개다. 유동성이 낮은 종목일수록
  시장충격이 커지므로, 최근 거래대금(ADV) 기준 sqrt(REF/ADV) 배수(1~IMPACT_CAP)를
  곱해 보수적으로 키운다. ADV가 REF 이상이면 배수 1.0(추가 충격 없음).
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
IMPACT_REF_VALUE = 3_000_000_000  # 슬리피지 배수 1.0 기준 ADV(원). 이상이면 충격 無
IMPACT_CAP = 5.0                   # 저유동성 종목 슬리피지 배수 상한
ADV_WINDOW = 20                   # ADV(평균 거래대금) 산정 일수


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
    slippage_bps: float = 10.0,
    stop_loss_pct: float = 0.0,
    trailing_pct: float = 0.0,
    require_financials: bool = True,
    regime_filter: bool = False,
    regime_ma: int = 200,
    start_date: str | None = None,
    end_date: str | None = None,
    max_pool: int | None = None,
    skip_top: int = 0,
    series: dict[str, list[dict]] | None = None,
    mom_days: int = 60,
    mom_skip: int = 5,
    weights: dict | None = None,
    vol_target: float = 0.0,
    vol_window: int = 60,
) -> dict[str, Any]:
    """mom_days/mom_skip: 모멘텀 룩백(기본 60/5, 학계 12-1은 230/20).
    weights: 팩터 가중치 주입(None=screener.WEIGHTS). 예: {"high_52w":0.2,...}
    vol_target: 연환산 목표 변동성 %(예 12). 0=비활성. 실현변동성이 목표를 넘으면
      노출(exposure)을 목표/실현 비율로 줄인다(레버리지 없음, 상한 1.0).
      Barroso & Santa-Clara(2015) 'Momentum Has Its Moments' 방식의 보수적 변형.
    skip_top: 유동성 최상위 N종목을 유니버스에서 제외(초대형주가 모멘텀을 방해한다는
      한국시장 연구(arXiv:1211.6517 universe shrinkage) 검증용). 0=비활성."""
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
    if (max_pool and len(pool) > max_pool) or skip_top:
        pool.sort(key=lambda s: liq_of.get(s, 0), reverse=True)
        if skip_top:
            pool = pool[skip_top:]
        if max_pool and len(pool) > max_pool:
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
    # 종목별 거래대금 배열 : 슬리피지(시장충격) 산정용 ADV 계산
    value_by: dict[str, list[int]] = {
        s: [r.get("value") or 0 for r in series[s]] for s in pool
    }

    def slip_mult_asof(s: str, t: str) -> float:
        """t시점 최근 ADV로 유동성 차등 슬리피지 배수(1.0~IMPACT_CAP)."""
        end = idx_by[s].get(t)
        if end is None or end < 1:
            return 1.0
        win = [v for v in value_by[s][max(0, end - ADV_WINDOW + 1): end + 1] if v > 0]
        if not win:
            return 1.0
        adv = sum(win) / len(win)
        if adv <= 0:
            return IMPACT_CAP
        return min(max((IMPACT_REF_VALUE / adv) ** 0.5, 1.0), IMPACT_CAP)

    # --- 벤치마크(지수) 배열: 레짐 필터 + 상대강도에 공통 사용 ---
    has_bench = BENCHMARK_SYMBOL in series
    bdates = [r["date"] for r in series[BENCHMARK_SYMBOL]] if has_bench else []
    bcloses = [r["close"] for r in series[BENCHMARK_SYMBOL]] if has_bench else []

    def _bench_pos(t: str) -> int:
        return bisect.bisect_right(bdates, t) - 1  # t 이하 마지막 지수 거래일

    def index_mom_asof(t: str) -> float | None:
        """t시점 지수 모멘텀(종목과 동일: 직전 mom_skip 제외 mom_days). 누출 없음."""
        if not has_bench:
            return None
        pos = _bench_pos(t)
        if pos < mom_days:
            return None
        return bcloses[pos - mom_skip] / bcloses[pos - mom_days] - 1

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
    slip = slippage_bps / 10_000.0
    slippage_drag = 0.0  # 슬리피지 누적 손실(투명성용)
    in_market_days = 0
    span_days = 0
    equity = 1.0
    bench_equity = 1.0
    curve: list[dict] = []
    holdings_log: list[dict] = []

    # 워밍업(모멘텀 룩백 충족) 또는 start_date 중 늦은 쪽에서 매매 시작
    start_i = max(60, mom_days)
    if start_date:
        start_i = max(start_i, bisect.bisect_left(dates, start_date))
    prev_holdings: list[str] = []

    # 변동성 타기팅: 전략 자체의 일별 수익률(노출 1.0 기준)로 실현변동성 추정
    vt = (vol_target / 100.0) if vol_target and vol_target > 0 else 0.0
    strat_rets: list[float] = []   # 일별 원수익률 기록(타기팅 입력)
    scaled_equity = 1.0            # 노출 조절 적용 자본곡선
    exposure = 1.0

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
            f = compute_factors(rows_asof, mom_days=mom_days, mom_skip=mom_skip)
            if f is None:
                continue
            f["symbol"] = s
            f["rel_strength"] = (f["momentum"] - idx_mom_t) if idx_mom_t is not None else f["momentum"]
            candidates.append(f)

        ranked = assign_scores(candidates, weights=weights)
        picks = [c["symbol"] for c in ranked[:top_n]]

        # 시장국면 필터: risk-off 면 현금 보유(picks 비움)
        if not is_risk_on(t):
            picks = []

        # 회전율 기반 거래비용 (교체된 비중만큼, 0~1 정규화)
        denom = max(len(picks), len(prev_holdings), 1)
        traded = set(picks) ^ set(prev_holdings)
        turnover = len(traded) / denom
        # 슬리피지: 매매된 종목별 유동성 차등 배수의 평균 × 회전율
        if traded and slip > 0:
            avg_slip = sum(slip * slip_mult_asof(s, t) for s in traded) / len(traded)
        else:
            avg_slip = 0.0
        # 변동성 타기팅: '직전까지'의 실현변동성으로 이번 구간 노출 결정(누출 없음)
        if vt > 0:
            win = strat_rets[-vol_window:]
            if len(win) >= 20:
                mu = sum(win) / len(win)
                var_d = max(sum((r - mu) ** 2 for r in win) / len(win), 0.0)
                rv = math.sqrt(var_d) * math.sqrt(252)
                exposure = min(1.0, vt / rv) if rv > 1e-9 else 1.0

        slip_rate = avg_slip * turnover
        slippage_drag += slip_rate
        equity *= (1 - cost * turnover - slip_rate)
        # 타기팅 곡선: 비용도 노출 비율만큼만 부담(투자분에만 발생)
        scaled_equity *= (1 - (cost * turnover + slip_rate) * exposure)
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

        # 청산(손절/트레일링) 사용 시: 종목별 자본을 따로 추적(청산분은 현금화).
        # 둘 다 0 이면 기존(일별 동일비중 리밸런싱) 경로를 그대로 사용해 과거 검증치 유지.
        # ※ 무청산 매칭 기준선이 필요하면 stop_loss_pct=999 처럼 큰 값을 주면
        #    같은 종목별 회계로 '발동 없는' 베이스라인이 된다(공정한 A/B용).
        use_exit = (stop_loss_pct > 0 or trailing_pct > 0) and picks
        if use_exit:
            stop_lvl = 1.0 - stop_loss_pct / 100.0 if stop_loss_pct > 0 else 0.0
            trail_lvl = 1.0 - trailing_pct / 100.0 if trailing_pct > 0 else 0.0
            name_eq = {s: 1.0 for s in picks}   # 진입가 대비 종목 자본배수
            peak_eq = {s: 1.0 for s in picks}   # 보유 중 최고 배수(트레일링 기준)
            active = {s: True for s in picks}
            base_port = prev_port               # 진입 시점 포트폴리오 가치

        for k in range(i + 1, j_end + 1):
            d = dates[k]
            port_before = prev_port  # 일별 원수익률 산출용(변동성 타기팅 입력)
            if use_exit:
                # 종목별 자본 갱신 + 손절/트레일링 청산(이후 현금=배수 고정)
                for s in picks:
                    if not active[s]:
                        continue
                    c0 = last_close.get(s)
                    c1 = close_by[s].get(d)
                    if c0 and c1:
                        name_eq[s] *= (c1 / c0)
                        last_close[s] = c1
                        if name_eq[s] > peak_eq[s]:
                            peak_eq[s] = name_eq[s]
                        if stop_loss_pct > 0 and name_eq[s] <= stop_lvl:
                            active[s] = False          # 손절
                        elif (trailing_pct > 0 and peak_eq[s] > 1.0
                              and name_eq[s] <= peak_eq[s] * trail_lvl):
                            active[s] = False          # 트레일링(올랐던 종목만)
                # 포트폴리오 = 동일비중(진입 1/N) × 종목 자본배수 평균 (청산분 freeze)
                prev_port = base_port * (sum(name_eq.values()) / len(picks))
            else:
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
            # 변동성 타기팅: 일별 수익률 기록 + 노출 조절 곡선 갱신
            r_day = (prev_port / port_before - 1) if port_before else 0.0
            strat_rets.append(r_day)
            scaled_equity *= (1 + exposure * r_day)
            # 벤치마크
            if bench_last:
                bc = close_by.get(BENCHMARK_SYMBOL, {}).get(d)
                if bc:
                    prev_bench *= (bc / bench_last)
                    bench_last = bc
            curve.append({"date": d,
                          "strategy": (scaled_equity if vt > 0 else prev_port),
                          "benchmark": prev_bench})

        window = j_end - i
        span_days += window
        if picks:
            in_market_days += window

        equity = prev_port
        bench_equity = prev_bench
        i = j_end if j_end > i else i + 1

    metrics = _metrics(curve)
    metrics["time_in_market"] = (in_market_days / span_days) if span_days else 1.0
    metrics["slippage_drag"] = slippage_drag  # 슬리피지로 인한 누적 손실(근사)
    return {
        "curve": curve,
        "metrics": metrics,
        "holdings_log": holdings_log,
        "params": {
            "top_n": top_n, "hold_days": hold_days, "cost_bps": cost_bps,
            "slippage_bps": slippage_bps, "stop_loss_pct": stop_loss_pct,
            "trailing_pct": trailing_pct,
            "regime_filter": regime_filter, "regime_ma": regime_ma,
            "mom_days": mom_days, "mom_skip": mom_skip,
            "weights": weights, "vol_target": vol_target,
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
    ap.add_argument("--cost-bps", type=float, default=25.0, help="수수료·세금(bps)")
    ap.add_argument("--slippage-bps", type=float, default=10.0,
                    help="슬리피지(bps, 유동성 낮을수록 자동 증폭). 0=비활성")
    ap.add_argument("--stop-loss-pct", type=float, default=0.0,
                    help="보유중 손절 %% (예: 8). 0=비활성. 켜면 손절분은 현금화")
    ap.add_argument("--trailing-pct", type=float, default=0.0,
                    help="보유중 고점 대비 트레일링스톱 %% (예: 15). 0=비활성")
    ap.add_argument("--skip-top", type=int, default=0,
                    help="유동성 최상위 N종목 제외(universe shrinkage 검증, 0=끔)")
    ap.add_argument("--max-pool", type=int, default=0,
                    help="유동성 상위 N종목으로 유니버스 제한(0=전체)")
    ap.add_argument("--include-etf", action="store_true")
    ap.add_argument("--regime-filter", action="store_true",
                    help="지수 이동평균 아래면 현금 보유(시장국면 필터)")
    ap.add_argument("--regime-ma", type=int, default=200, help="국면 판정 이동평균 일수")
    ap.add_argument("--start-date", type=str, default=None, help="매매 시작일(YYYYMMDD)")
    ap.add_argument("--mom-days", type=int, default=60, help="모멘텀 룩백 일수(학계 12-1은 230)")
    ap.add_argument("--mom-skip", type=int, default=5, help="모멘텀 스킵 일수(12-1은 20)")
    ap.add_argument("--w-high52", type=float, default=0.0,
                    help="52주 신고가 근접 팩터 가중치(예 0.2). 모멘텀 가중에서 차감")
    ap.add_argument("--vol-target", type=float, default=0.0,
                    help="연 목표변동성 %%(예 12). 실현변동성 초과 시 노출 축소. 0=OFF")
    args = ap.parse_args()

    # --w-high52 지정 시: 모멘텀 가중에서 그만큼 떼어 52주 신고가에 배분(합 1.0 유지)
    weights = None
    if args.w_high52 > 0:
        from .screener import WEIGHTS
        weights = dict(WEIGHTS)
        weights["momentum"] = max(weights.get("momentum", 0.0) - args.w_high52, 0.0)
        weights["high_52w"] = args.w_high52

    store = DataStore()
    res = run_backtest(
        store, top_n=args.top_n, hold_days=args.hold_days,
        cost_bps=args.cost_bps, slippage_bps=args.slippage_bps,
        stop_loss_pct=args.stop_loss_pct, trailing_pct=args.trailing_pct,
        max_pool=(args.max_pool or None), skip_top=args.skip_top,
        require_financials=not args.include_etf,
        regime_filter=args.regime_filter, regime_ma=args.regime_ma,
        start_date=args.start_date,
        mom_days=args.mom_days, mom_skip=args.mom_skip,
        weights=weights, vol_target=args.vol_target,
    )
    store.close()

    if "error" in res:
        print(res["error"])
        return
    m = res["metrics"]
    rf = "ON" if res["params"]["regime_filter"] else "OFF"
    p = res["params"]
    print(f"대상 종목 풀: {res['pool_size']}개 / 국면필터 {rf}(적용={res['regime_applied']})")
    sl = p.get("stop_loss_pct", 0)
    tr = p.get("trailing_pct", 0)
    print(f"거래비용   : 수수료·세금 {p['cost_bps']:.0f}bps + 슬리피지 {p['slippage_bps']:.0f}bps(유동성차등)")
    print(f"청산       : 손절 {('%.0f%%' % sl) if sl > 0 else 'OFF'} · "
          f"트레일링 {('%.0f%%' % tr) if tr > 0 else 'OFF'}")
    print(f"누적수익률 : {m['total_return']*100:+.1f}%  (벤치마크 {m['bench_return']*100:+.1f}%)")
    print(f"CAGR       : {m['cagr']*100:+.1f}%")
    print(f"샤프지수   : {m['sharpe']:.2f}")
    print(f"최대낙폭   : {m['mdd']*100:.1f}%")
    print(f"연변동성   : {m['volatility']*100:.1f}%")
    print(f"투자기간비율: {m['time_in_market']*100:.0f}%  (나머지는 현금)")
    print(f"슬리피지 손실: 누적 약 -{m['slippage_drag']*100:.1f}%p (체결 밀림 추정)")


if __name__ == "__main__":
    main()
