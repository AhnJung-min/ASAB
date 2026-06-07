"""v1 전략 맞대결 — screener vs ml vs blend (동일 OOS·국면·비용).

부품(점수)들을 같은 조건에서 비교해 라이브 v1을 데이터로 확정한다.
features.build_dataset 패널(피처+fwd_ret)을 공유 입력으로 쓰며, 같은
워크포워드 폴드(embargo)·같은 횡단면·같은 거래비용·같은 시장국면 게이트를
적용한다 → 사과 대 사과.

  screener : 4팩터(모멘텀0.4·추세0.2·유동성0.2·저변동0.2) 횡단면 백분위 합
  ml       : 폴드별 학습 LightGBM 예측(미래수익률)
  blend    : screener 백분위 0.5 + ml 백분위 0.5

국면: 지수(KODEX200) 종가가 200일선 아래인 리밸런스는 현금(수익 0).
비용: 직전 리밸런스 대비 교체분 회전율 × cost_bps(라운드트립).

사용:  python -m src.strategy_compare --top-n 5
       python -m src.strategy_compare --no-regime --cost-bps 35
"""
from __future__ import annotations

import argparse
import math
import sys
import warnings

warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

from .data.store import DataStore
from .features import BENCHMARK_SYMBOL, FEATURES, build_dataset

TRAIN_PERIODS, TEST_PERIODS, EMBARGO = 36, 6, 1
REGIME_MA = 200


def _pct(vals: list[float]) -> np.ndarray:
    """값 → 0~1 백분위(동률 평균)."""
    a = np.asarray(vals, dtype=float)
    order = a.argsort()
    ranks = np.empty(len(a))
    ranks[order] = np.arange(len(a))
    return ranks / (len(a) - 1) if len(a) > 1 else np.full(len(a), 0.5)


def _screener_score(rows: list[dict]) -> np.ndarray:
    """피처 기반 스크리너 점수(횡단면 백분위 가중합). 저변동은 낮을수록 좋아 -vol_60."""
    return (0.40 * _pct([r["ret_60"] for r in rows])
            + 0.20 * _pct([r["ma20_ratio"] for r in rows])
            + 0.20 * _pct([r["liq"] for r in rows])
            + 0.20 * _pct([-r["vol_60"] for r in rows]))


def _metrics(rets: list[float], ppy: float = 12.0) -> dict:
    if not rets:
        return {"n": 0}
    eq, v = [1.0], 1.0
    for r in rets:
        v *= (1 + r); eq.append(v)
    mean = sum(rets) / len(rets)
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    peak, mdd = 1.0, 0.0
    for x in eq:
        peak = max(peak, x); mdd = min(mdd, x / peak - 1)
    years = len(rets) / ppy
    return {"n": len(rets), "total": eq[-1] - 1,
            "cagr": eq[-1] ** (1 / years) - 1 if years > 0 else 0.0,
            "sharpe": (mean / std * math.sqrt(ppy)) if std > 0 else 0.0,
            "mdd": mdd, "hit": sum(1 for r in rets if r > 0) / len(rets)}


def _regime_map(store: DataStore, dates: list[str]) -> dict[str, bool]:
    rows = store.get_daily(BENCHMARK_SYMBOL)
    bdates = [r["date"] for r in rows]
    bclose = [r["close"] for r in rows]
    import bisect
    out = {}
    for d in dates:
        pos = bisect.bisect_right(bdates, d) - 1
        if pos >= REGIME_MA - 1:
            ma = sum(bclose[pos - REGIME_MA + 1: pos + 1]) / REGIME_MA
            out[d] = bclose[pos] > ma
        else:
            out[d] = True
    return out


def run_compare(store: DataStore, dataset, top_n=5, cost_bps=35.0, regime=True) -> dict:
    import lightgbm as lgb
    cost = cost_bps / 10_000.0
    dates = sorted({r["date"] for r in dataset})
    by_date: dict[str, list] = {}
    for r in dataset:
        by_date.setdefault(r["date"], []).append(r)
    reg = _regime_map(store, dates) if regime else {d: True for d in dates}

    methods = ["screener", "ml", "blend"]
    rets = {m: [] for m in methods}
    prev = {m: set() for m in methods}

    k = 0
    while k + TRAIN_PERIODS + EMBARGO + TEST_PERIODS <= len(dates):
        tr = [r for d in dates[k:k + TRAIN_PERIODS] for r in by_date[d]]
        Xtr = np.array([[r[f] for f in FEATURES] for r in tr], float)
        ytr = np.array([r["fwd_ret"] for r in tr], float)
        model = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.03, num_leaves=15,
                                  min_child_samples=50, subsample=0.8,
                                  colsample_bytree=0.8, reg_lambda=1.0, verbose=-1)
        model.fit(Xtr, ytr)
        te0 = k + TRAIN_PERIODS + EMBARGO
        for d in dates[te0:te0 + TEST_PERIODS]:
            rows = by_date[d]
            fwd = np.array([r["fwd_ret"] for r in rows])
            scr = _screener_score(rows)
            ml = model.predict(np.array([[r[f] for f in FEATURES] for r in rows], float))
            blend = 0.5 * _pct(list(scr)) + 0.5 * _pct(list(ml))
            scores = {"screener": scr, "ml": np.asarray(ml), "blend": blend}
            for m in methods:
                if not reg[d]:
                    rets[m].append(0.0); prev[m] = set(); continue
                idx = np.argsort(scores[m])[::-1][:top_n]
                picks = {rows[i]["symbol"] for i in idx}
                gross = float(np.mean([fwd[i] for i in idx]))
                turn = len(picks ^ prev[m]) / max(len(picks), 1)
                rets[m].append(gross - cost * turn)
                prev[m] = picks
        k += TEST_PERIODS
    return {m: _metrics(rets[m]) for m in methods}


def main() -> None:
    ap = argparse.ArgumentParser(description="v1 전략 맞대결(screener/ml/blend)")
    ap.add_argument("--top-n", type=int, nargs="+", default=[5, 10])
    ap.add_argument("--max-pool", type=int, default=200)
    ap.add_argument("--hold-days", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=35.0)
    ap.add_argument("--no-regime", action="store_true")
    args = ap.parse_args()

    store = DataStore()
    print("데이터셋 생성 중...", flush=True)
    ds = build_dataset(store, hold_days=args.hold_days, max_pool=args.max_pool)
    print(f"  {len(ds):,}행\n", flush=True)
    reg = not args.no_regime
    print(f"국면필터={'ON' if reg else 'OFF'} · 비용 {args.cost_bps:.0f}bps · 유니버스 상위{args.max_pool}")
    print(f"{'설정':18}{'누적':>9}{'CAGR':>8}{'샤프':>7}{'MDD':>8}{'승률':>7}")
    print("-" * 60)
    best = None
    for tn in args.top_n:
        res = run_compare(store, ds, top_n=tn, cost_bps=args.cost_bps, regime=reg)
        for m in ("screener", "ml", "blend"):
            x = res[m]
            print(f"{m+' top'+str(tn):18}{x['total']*100:>8.0f}%{x['cagr']*100:>7.1f}%"
                  f"{x['sharpe']:>7.2f}{x['mdd']*100:>7.0f}%{x['hit']*100:>6.0f}%")
            if best is None or x["sharpe"] > best[1]:
                best = (f"{m} top{tn}", x["sharpe"])
        print("-" * 60)
    store.close()
    print(f"\n→ 샤프 최고: {best[0]} ({best[1]:.2f}) — v1 후보")


if __name__ == "__main__":
    main()
