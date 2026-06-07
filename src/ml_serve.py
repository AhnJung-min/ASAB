"""ML 종합점수 서빙 — 학습된 LightGBM으로 현재 유니버스에 점수를 매긴다.

워크포워드(ml.py)가 'ML이 모멘텀보다 OOS 우위'를 검증했으므로, 그 모델을
실거래 종목선정에 연결한다. 14개 가격기반 지표(features.FEATURES)를 융합한
'종합지표' 역할.

  train()            : 전체 일봉으로 LightGBM 학습 → data/ml_model.pkl 저장
  predict_universe() : 현재 시점 as-of 피처로 유니버스 점수 예측(내림차순)

사용:  python -m src.ml_serve --train        # 모델 학습/저장
       python -m src.ml_serve --top 10       # 현재 ML 상위 종목 미리보기
"""
from __future__ import annotations

import argparse
import pickle
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")  # sklearn 피처명 경고 등 억제

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

from .data.store import DataStore
from .features import (BENCHMARK_SYMBOL, FEATURES, WARMUP, build_dataset,
                       compute_features)

MODEL_PATH = Path("data") / "ml_model.pkl"


def train(store: DataStore, hold_days: int = 20, max_pool: int = 300) -> dict[str, Any]:
    """전체 히스토리로 학습 후 모델 저장. 메타 반환."""
    import lightgbm as lgb
    ds = build_dataset(store, hold_days=hold_days, max_pool=max_pool)
    if not ds:
        return {"error": "학습 데이터 없음. 먼저 데이터를 수집하세요."}
    X = np.array([[r[f] for f in FEATURES] for r in ds], dtype=float)
    y = np.array([r["fwd_ret"] for r in ds], dtype=float)
    model = lgb.LGBMRegressor(
        n_estimators=300, learning_rate=0.03, num_leaves=15, min_child_samples=50,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0, verbose=-1)
    model.fit(X, y)
    meta = {"rows": len(ds), "span": (ds[0]["date"], ds[-1]["date"]),
            "hold_days": hold_days, "max_pool": max_pool}
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": model, "features": FEATURES, "meta": meta}, f)
    return meta


def load_model() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def _idx_ret60(store: DataStore) -> float | None:
    rows = store.get_daily(BENCHMARK_SYMBOL)
    c = [r["close"] for r in rows]
    return (c[-1] / c[-61] - 1) if len(c) >= 61 else None


def predict_universe(store: DataStore, max_pool: int = 300) -> list[dict[str, Any]] | None:
    """현재(as-of 최신) 피처로 유니버스 ML 점수 예측. 모델 없으면 None.

    반환: [{symbol, name, ml_score, last_close}] ml_score 내림차순.
    """
    bundle = load_model()
    if not bundle:
        return None
    model, feats = bundle["model"], bundle["features"]
    ir = _idx_ret60(store)
    master = store.master_symbols(by_liquidity=True)
    pool = [m["symbol"] for m in master][: max_pool * 2]  # 유동성 상위 여유있게

    out: list[dict[str, Any]] = []
    for sym in pool:
        rows = store.get_daily(sym)
        if len(rows) < WARMUP:
            continue
        closes = [r["close"] for r in rows]
        values = [r["value"] for r in rows]
        volumes = [r["volume"] for r in rows]
        f = compute_features(closes, values, volumes, ir)
        if f is None:
            continue
        X = np.array([[f[k] for k in feats]], dtype=float)
        out.append({"symbol": sym, "name": store.name_of(sym),
                    "ml_score": float(model.predict(X)[0]), "last_close": closes[-1]})
        if len(out) >= max_pool:
            break
    out.sort(key=lambda d: -d["ml_score"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="ML 종합점수 서빙")
    ap.add_argument("--train", action="store_true", help="모델 학습/저장")
    ap.add_argument("--max-pool", type=int, default=300)
    ap.add_argument("--hold-days", type=int, default=20)
    ap.add_argument("--top", type=int, default=10, help="예측 상위 N 미리보기")
    args = ap.parse_args()

    store = DataStore()
    if args.train:
        print("모델 학습 중...", flush=True)
        meta = train(store, hold_days=args.hold_days, max_pool=args.max_pool)
        if "error" in meta:
            print(meta["error"]); store.close(); return
        print(f"  저장: {MODEL_PATH} · 학습 {meta['rows']:,}행 "
              f"({meta['span'][0]}~{meta['span'][1]})")

    preds = predict_universe(store, max_pool=args.max_pool)
    store.close()
    if preds is None:
        print("저장된 모델이 없습니다. 먼저 --train 하세요.")
        return
    print(f"\nML 종합점수 상위 {args.top} (예측 미래수익률 기준)")
    print(f"{'순위':>2} {'종목명':<16} {'코드':<8} {'ML점수':>9} {'종가':>9}")
    print("-" * 50)
    for i, p in enumerate(preds[:args.top], 1):
        print(f"{i:>2} {p['name']:<16} {p['symbol']:<8} {p['ml_score']*100:>8.2f}% {p['last_close']:>9,}")


if __name__ == "__main__":
    main()
