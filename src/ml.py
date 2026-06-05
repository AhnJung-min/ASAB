"""LightGBM 워크포워드 학습·검증.

데이터셋(피처+미래수익률)을 시간순 폴드로 굴리며:
  1) 과거 구간(train)으로 LightGBM 학습(미래수익률 예측)
  2) 본 적 없는 구간(test)에서 예측 → 상위 N 종목 선택 → 실현수익 측정
검증구간만 이어붙여 ML의 진짜 OOS 성과를 추정하고,
모멘텀(단순 ret_60) 베이스라인 및 시장(전체평균)과 비교한다.

핵심 질문: ML이 규칙기반(모멘텀)보다 정말 알파를 더 내는가?
"""
from __future__ import annotations

import argparse
import math
import sys
import warnings

warnings.filterwarnings("ignore")  # sklearn 피처명 경고 등 억제

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

from .data.store import DataStore
from .features import FEATURES, build_dataset

TRAIN_PERIODS = 36   # 학습 리밸런스 수 (~3년)
TEST_PERIODS = 6     # 검증 리밸런스 수 (~6개월)
TOP_N = 10


def _metrics(period_rets: list[float], ppy: float = 12.0) -> dict:
    """리밸런스(약 월)별 수익률 리스트 → 지표. ppy=연간 리밸런스 수."""
    if not period_rets:
        return {}
    eq = [1.0]
    for r in period_rets:
        eq.append(eq[-1] * (1 + r))
    total = eq[-1] - 1
    years = len(period_rets) / ppy
    cagr = eq[-1] ** (1 / years) - 1 if years > 0 else 0.0
    mean = sum(period_rets) / len(period_rets)
    std = math.sqrt(sum((r - mean) ** 2 for r in period_rets) / len(period_rets))
    sharpe = (mean / std * math.sqrt(ppy)) if std > 0 else 0.0
    peak, mdd = 1.0, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total": total, "cagr": cagr, "sharpe": sharpe, "mdd": mdd}


def run_ml_wf(dataset, train_periods=TRAIN_PERIODS, test_periods=TEST_PERIODS,
              top_n=TOP_N) -> dict:
    import lightgbm as lgb

    dates = sorted({d["date"] for d in dataset})
    by_date = {}
    for row in dataset:
        by_date.setdefault(row["date"], []).append(row)
    if len(dates) < train_periods + test_periods:
        return {"error": f"리밸런스 기간 부족({len(dates)} < {train_periods+test_periods}). "
                "데이터가 더 필요합니다."}

    ml_rets, mom_rets, mkt_rets = [], [], []
    test_span = []
    k = 0
    while k + train_periods + test_periods <= len(dates):
        tr_dates = dates[k:k + train_periods]
        te_dates = dates[k + train_periods:k + train_periods + test_periods]

        # 학습셋
        tr = [r for d in tr_dates for r in by_date[d]]
        Xtr = np.array([[row[f] for f in FEATURES] for row in tr], dtype=float)
        ytr = np.array([row["fwd_ret"] for row in tr], dtype=float)
        model = lgb.LGBMRegressor(
            n_estimators=200, learning_rate=0.03, num_leaves=15,
            min_child_samples=50, subsample=0.8, colsample_bytree=0.8,
            reg_lambda=1.0, verbose=-1,
        )
        model.fit(Xtr, ytr)

        # 검증구간: 각 날짜별 예측→상위N 선택→실현수익
        for d in te_dates:
            rows = by_date[d]
            X = np.array([[row[f] for f in FEATURES] for row in rows], dtype=float)
            pred = model.predict(X)
            order = np.argsort(pred)[::-1]
            picks = order[:top_n]
            ml_rets.append(float(np.mean([rows[i]["fwd_ret"] for i in picks])))
            # 모멘텀 베이스라인(ret_60 상위)
            mom_order = sorted(range(len(rows)), key=lambda i: rows[i]["ret_60"], reverse=True)
            mom_rets.append(float(np.mean([rows[i]["fwd_ret"] for i in mom_order[:top_n]])))
            # 시장(전체 평균)
            mkt_rets.append(float(np.mean([r["fwd_ret"] for r in rows])))
            test_span.append(d)
        k += test_periods

    def _cum(rets):
        eq, v = [], 1.0
        for r in rets:
            v *= (1 + r); eq.append(v)
        return eq

    # 피처 중요도(전체 데이터 학습)
    Xall = np.array([[r[f] for f in FEATURES] for r in dataset], dtype=float)
    yall = np.array([r["fwd_ret"] for r in dataset], dtype=float)
    fm = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.03, num_leaves=15,
                           min_child_samples=50, verbose=-1).fit(Xall, yall)
    importance = sorted(zip(FEATURES, [int(v) for v in fm.feature_importances_]),
                        key=lambda t: -t[1])

    return {
        "ml": _metrics(ml_rets), "mom": _metrics(mom_rets), "mkt": _metrics(mkt_rets),
        "n_test": len(ml_rets), "span": (test_span[0], test_span[-1]) if test_span else None,
        "dataset_rows": len(dataset),
        "curves": {"dates": test_span, "ml": _cum(ml_rets),
                   "mom": _cum(mom_rets), "mkt": _cum(mkt_rets)},
        "importance": importance,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="LightGBM 워크포워드 검증")
    ap.add_argument("--hold-days", type=int, default=20)
    ap.add_argument("--max-pool", type=int, default=300)
    ap.add_argument("--top-n", type=int, default=TOP_N)
    args = ap.parse_args()

    store = DataStore()
    print("데이터셋 생성 중...", flush=True)
    ds = build_dataset(store, hold_days=args.hold_days, max_pool=args.max_pool)
    store.close()
    print(f"  {len(ds):,}행", flush=True)

    res = run_ml_wf(ds, top_n=args.top_n)
    if "error" in res:
        print(res["error"]); return

    print(f"\n검증 OOS: {res['n_test']}기간 ({res['span'][0]}~{res['span'][1]})")
    print(f"{'':12}{'누적':>9}{'CAGR':>9}{'샤프':>7}{'MDD':>8}")
    for key, label in (("ml", "ML(LGBM)"), ("mom", "모멘텀"), ("mkt", "시장(평균)")):
        m = res[key]
        print(f"{label:12}{m['total']*100:>8.1f}%{m['cagr']*100:>8.1f}%{m['sharpe']:>7.2f}{m['mdd']*100:>7.1f}%")
    print("\n→ ML이 모멘텀·시장보다 샤프/수익이 높아야 '알파'가 있는 것입니다.")
    print("\n피처 중요도(상위):")
    for name, val in res["importance"][:8]:
        print(f"   {name:<16}{val}")


if __name__ == "__main__":
    main()
