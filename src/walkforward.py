"""워크포워드(Walk-Forward) 검증.

전체 기간을 (학습구간 → 검증구간) 폴드로 굴리며:
  1) 학습구간에서 파라미터 그리드 중 최고(Sharpe)를 고르고
  2) 그 설정을 '본 적 없는' 검증구간(out-of-sample)에 적용
검증구간 성과만 이어붙여 '진짜 실력(OOS)'을 추정한다.
인샘플 최적(전구간을 보고 고른 최고)과 비교해 **과적합 정도**를 드러낸다.

핵심: 인샘플은 항상 좋아 보인다. WF(OOS)가 인샘플보다 크게 나쁘면 과적합 신호.
"""
from __future__ import annotations

import argparse
import math
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .backtest import BENCHMARK_SYMBOL, _load_series, run_backtest
from .data.store import DataStore

# 파라미터 그리드 (작게 유지 — 그리드 자체의 과적합 방지)
GRID = [
    {"top_n": n, "regime_filter": r}
    for n in (3, 5, 10)
    for r in (False, True)
]
TRAIN_DAYS = 504   # 학습 ~2년
TEST_DAYS = 126    # 검증 ~6개월
MAX_POOL = 200     # 유동성 상위 200종목
HOLD = 20
COST = 25.0


def _calendar(series: dict) -> list[str]:
    if BENCHMARK_SYMBOL in series:
        return [r["date"] for r in series[BENCHMARK_SYMBOL]]
    dates: set[str] = set()
    for rows in series.values():
        dates.update(r["date"] for r in rows)
    return sorted(dates)


def _metrics_from_equity(eq: list[float], bench: list[float]) -> dict:
    if len(eq) < 2:
        return {}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    years = len(eq) / 252
    cagr = eq[-1] ** (1 / years) - 1 if years > 0 else 0.0
    mean = sum(rets) / len(rets)
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total_return": eq[-1] - 1, "cagr": cagr, "sharpe": sharpe,
            "mdd": mdd, "bench_return": (bench[-1] - 1) if len(bench) > 1 else 0.0}


def run_wf(store: DataStore, train_days=TRAIN_DAYS, test_days=TEST_DAYS,
           max_pool=MAX_POOL) -> dict:
    series = _load_series(store)
    if not series:
        return {"error": "데이터가 없습니다."}
    cal = _calendar(series)
    start = 220  # 워밍업(200일선+여유) 후 시작
    folds = []
    k = start
    while k + train_days + test_days <= len(cal):
        folds.append((cal[k], cal[k + train_days - 1],
                      cal[k + train_days], cal[k + train_days + test_days - 1]))
        k += test_days
    if not folds:
        return {"error": f"기간 부족: 폴드 생성 불가 (거래일 {len(cal)}일). "
                "데이터가 더 쌓이면 다시 시도하세요."}

    def bt(cfg, s, e):
        return run_backtest(store, hold_days=HOLD, cost_bps=COST, max_pool=max_pool,
                            start_date=s, end_date=e, series=series, **cfg)

    fold_rows = []
    eq, beq = [1.0], [1.0]
    cur, bcur = 1.0, 1.0
    for (tr0, tr1, te0, te1) in folds:
        # 1) 학습구간에서 그리드 중 최고(Sharpe) 선택
        best = None
        for cfg in GRID:
            r = bt(cfg, tr0, tr1)
            sh = r.get("metrics", {}).get("sharpe", -99) if "error" not in r else -99
            if best is None or sh > best[0]:
                best = (sh, cfg)
        cfg = best[1]
        # 2) 검증구간(OOS) 적용
        oos = bt(cfg, te0, te1)
        if "error" in oos or not oos.get("curve"):
            continue
        m = oos["metrics"]
        fold_rows.append({
            "train": f"{tr0[:6]}~{tr1[:6]}", "test": f"{te0[:6]}~{te1[:6]}",
            "cfg": f"top{cfg['top_n']}{'+국면' if cfg['regime_filter'] else ''}",
            "oos_ret": m["total_return"], "oos_sharpe": m["sharpe"],
            "bench": m["bench_return"],
        })
        # OOS 곡선 이어붙이기(복리)
        for p in oos["curve"]:
            eq.append(cur * p["strategy"])
            beq.append(bcur * p["benchmark"])
        cur = eq[-1]
        bcur = beq[-1]

    if not fold_rows:
        return {"error": "검증 가능한 폴드가 없습니다(데이터 부족)."}

    wf = _metrics_from_equity(eq, beq)

    # 비교용 인샘플 최적: 전체 OOS 기간을 '보고' 고른 최고 설정
    span0, span1 = folds[0][2], folds[-1][3]
    is_best = None
    for cfg in GRID:
        r = bt(cfg, span0, span1)
        if "error" in r:
            continue
        sh = r["metrics"]["sharpe"]
        if is_best is None or sh > is_best[0]:
            is_best = (sh, cfg, r["metrics"])

    return {"folds": fold_rows, "wf": wf, "span": (span0, span1),
            "is_best": (is_best[1], is_best[2]) if is_best else None,
            "n_folds": len(fold_rows)}


def main() -> None:
    ap = argparse.ArgumentParser(description="워크포워드 검증")
    ap.add_argument("--train-days", type=int, default=TRAIN_DAYS)
    ap.add_argument("--test-days", type=int, default=TEST_DAYS)
    ap.add_argument("--max-pool", type=int, default=MAX_POOL)
    args = ap.parse_args()

    store = DataStore()
    res = run_wf(store, args.train_days, args.test_days, args.max_pool)
    store.close()
    if "error" in res:
        print(res["error"])
        return

    print(f"\n{'학습구간':<16}{'검증구간':<16}{'선택설정':<14}{'OOS수익':>9}{'샤프':>7}{'벤치':>9}")
    print("-" * 75)
    for f in res["folds"]:
        print(f"{f['train']:<16}{f['test']:<16}{f['cfg']:<14}"
              f"{f['oos_ret']*100:>8.1f}%{f['oos_sharpe']:>7.2f}{f['bench']*100:>8.1f}%")
    wf = res["wf"]
    print("\n===== 워크포워드(진짜 OOS) 종합 =====")
    print(f"  폴드 수      : {res['n_folds']}  ({res['span'][0][:6]}~{res['span'][1][:6]})")
    print(f"  누적수익률   : {wf['total_return']*100:+.1f}%  (벤치 {wf['bench_return']*100:+.1f}%)")
    print(f"  CAGR         : {wf['cagr']*100:+.1f}%")
    print(f"  샤프지수     : {wf['sharpe']:.2f}")
    print(f"  최대낙폭     : {wf['mdd']*100:.1f}%")
    if res["is_best"]:
        cfg, m = res["is_best"]
        print("\n----- 참고: 인샘플 최적(전구간 보고 고른 것 — 낙관 편향) -----")
        print(f"  설정 top{cfg['top_n']}{'+국면' if cfg['regime_filter'] else ''}: "
              f"누적 {m['total_return']*100:+.1f}%, 샤프 {m['sharpe']:.2f}")
        print("  → WF(OOS)가 이보다 크게 낮으면 과적합 신호입니다.")


if __name__ == "__main__":
    main()
