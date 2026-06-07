"""피처 ablation — 후보 보조지표를 하나씩 넣어 ML 성과 변화 측정.

원칙: 무작정 넣지 않는다. baseline(현 14피처) 대비 'FEATURES + 후보 1개'로
워크포워드(embargo)를 돌려 샤프가 오르면 채택, 아니면 기각(과적합 방지).
조건은 v1과 동일(ml top5 · 국면필터 · 거래비용).

사용:  python -m src.feature_lab               # 후보 전체 하나씩 ablation
       python -m src.feature_lab --top-n 5
"""
from __future__ import annotations

import argparse
import sys
import warnings

warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

from .data.store import DataStore
from .features import CANDIDATE_FEATURES, FEATURES, build_dataset
from .strategy_compare import (EMBARGO, TEST_PERIODS, TRAIN_PERIODS, _metrics,
                               _regime_map)


def ml_wf(dataset, feat_list, reg, top_n=5, cost_bps=35.0) -> dict:
    """주어진 피처 리스트로 ML 워크포워드(embargo) → top_n·국면·비용 반영 지표."""
    import lightgbm as lgb
    cost = cost_bps / 10_000.0
    dates = sorted({r["date"] for r in dataset})
    by_date: dict[str, list] = {}
    for r in dataset:
        by_date.setdefault(r["date"], []).append(r)
    rets, prev = [], set()
    k = 0
    while k + TRAIN_PERIODS + EMBARGO + TEST_PERIODS <= len(dates):
        tr = [r for d in dates[k:k + TRAIN_PERIODS] for r in by_date[d]]
        Xtr = np.array([[r[f] for f in feat_list] for r in tr], float)
        ytr = np.array([r["fwd_ret"] for r in tr], float)
        model = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.03, num_leaves=15,
                                  min_child_samples=50, subsample=0.8,
                                  colsample_bytree=0.8, reg_lambda=1.0, verbose=-1)
        model.fit(Xtr, ytr)
        te0 = k + TRAIN_PERIODS + EMBARGO
        for d in dates[te0:te0 + TEST_PERIODS]:
            rows = by_date[d]
            if not reg.get(d, True):
                rets.append(0.0); prev = set(); continue
            pred = model.predict(np.array([[r[f] for f in feat_list] for r in rows], float))
            idx = np.argsort(pred)[::-1][:top_n]
            picks = {rows[i]["symbol"] for i in idx}
            gross = float(np.mean([rows[i]["fwd_ret"] for i in idx]))
            turn = len(picks ^ prev) / max(len(picks), 1)
            rets.append(gross - cost * turn)
            prev = picks
        k += TEST_PERIODS
    return _metrics(rets)


def main() -> None:
    ap = argparse.ArgumentParser(description="피처 ablation(후보 보조지표)")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--max-pool", type=int, default=200)
    ap.add_argument("--cost-bps", type=float, default=35.0)
    args = ap.parse_args()

    store = DataStore()
    print("데이터셋 생성 중(고가/저가 포함)...", flush=True)
    ds = build_dataset(store, max_pool=args.max_pool)
    reg = _regime_map(store, sorted({r["date"] for r in ds}))
    store.close()
    print(f"  {len(ds):,}행 · 후보 {CANDIDATE_FEATURES}\n", flush=True)

    base = ml_wf(ds, FEATURES, reg, top_n=args.top_n, cost_bps=args.cost_bps)
    print(f"{'피처셋':22}{'샤프':>7}{'ΔSharpe':>9}{'CAGR':>8}{'MDD':>8}")
    print("-" * 56)
    print(f"{'baseline(14)':22}{base['sharpe']:>7.2f}{'':>9}{base['cagr']*100:>7.1f}%{base['mdd']*100:>7.0f}%")
    print("-" * 56)
    rows = []
    for cand in CANDIDATE_FEATURES:
        r = ml_wf(ds, FEATURES + [cand], reg, top_n=args.top_n, cost_bps=args.cost_bps)
        d = r["sharpe"] - base["sharpe"]
        rows.append((cand, d, r))
        flag = "  ✅채택후보" if d > 0.02 else ("  ⚠️미미" if d > -0.02 else "  ❌기각")
        print(f"{'+'+cand:22}{r['sharpe']:>7.2f}{d:>+9.2f}{r['cagr']*100:>7.1f}%{r['mdd']*100:>7.0f}%{flag}")
    print("-" * 56)
    win = [c for c, d, _ in rows if d > 0.02]
    print(f"\n채택 후보(ΔSharpe>+0.02): {win or '없음'}")
    print("해석: 단독 추가로 샤프 오르는 것만 1차 후보. 다음엔 함께 넣어 재검증.")


if __name__ == "__main__":
    main()
