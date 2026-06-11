"""급등주 단타 학습 — 스캔 스냅샷으로 '진입 후 N분 수익'을 예측하는 모델.

로드맵 5단계. 일봉 ML(ml_serve.py)과 같은 구조를 단타에 적용한다.

데이터 두 갈래(사용자 선택: 스캔+매매 둘 다):
  ① 학습원천 = surge_scan : 매 주기 저장된 등락률 상위 종목들에 forward-return
     (그 종목의 N분 뒤 가격/현재가-1)을 라벨로 붙여 '어떤 급등이 더 오르나' 학습.
     하루 수백 행이라 빨리 쌓인다. (한계: 상위권에 머문 종목 위주=생존편향,
     실제 체결/슬리피지/비용 미반영=낙관적)
  ② 검증 = surge_trade : 봇이 실제 체결한 매매의 익절/손절 결과로 교차 확인.

핵심 원칙(이 프로젝트의 일관된 규율): **데이터가 충분할 때만 학습한다.**
표본이 적으면 학습을 거부한다(과적합 = 손절휩쏘·수출+8% 가짜엣지와 같은 함정).

  python -m src.surge_ml              # 현재 데이터로 학습 가능 여부/통계
  python -m src.surge_ml --train      # 모델 학습·저장(데이터 충분 시)
  python -m src.surge_ml --horizon 30 # forward-return 지평(분)
"""
from __future__ import annotations

import argparse
import math
import pickle
import sys
import warnings
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore

MODEL_PATH = Path("data") / "surge_model.pkl"

# 오프라인(스캔)·온라인(라이브) 양쪽에서 동일하게 계산 가능한 피처만 사용한다.
# ob_imbalance(호가 잔량 임밸런스)는 단타 핵심 피처(상위 후보만 수집 → 없으면 NaN).
# index_chg(시장 국면=지수 등락률)는 Track1에서 검증된 최강 방어 신호.
# rvol(상대거래량)은 'Stocks in Play' 근거(Zarattini 외 2024) — 구 데이터는 NaN
# (Ridge는 Imputer, LightGBM은 네이티브로 결측 처리. 구 모델은 자기 피처목록 사용).
FEATURES = ["rate", "log_volume", "log_price", "hour", "weekday", "rank",
            "n_scan", "ob_imbalance", "index_chg", "rvol"]

MIN_SAMPLES = 300        # 선형모델 최소 표본(이 미만이면 학습 거부)
LGBM_MIN = 3000          # LightGBM(트리)은 이만큼 모여야 — 소데이터엔 과적합
DEFAULT_HORIZON_MIN = 30
# 왕복 거래비용(매수수수료+매도수수료+거래세) 근사 + 슬리피지(원하는 가격에 못 받는 손해).
# forward-return 라벨에서 차감해 '진짜 수익'으로 학습한다(시뮬 수익이 실전서 녹는 함정 방지).
DEFAULT_COST_BPS = 40.0
DEFAULT_SLIPPAGE_BPS = 30.0   # 진입+청산 합 약 1~2호가 손해 가정(보수적)
_TOL = timedelta(seconds=120)   # forward 가격 매칭 허용오차(폴링 30s 기준)


def feat_row(rate: float, volume: float, price: float, rank: int | None,
             n_scan: int, t: datetime, ob_imbalance: float | None = None,
             index_chg: float | None = None,
             rvol: float | None = None) -> dict[str, float]:
    """피처 한 행. 라이브 후보와 과거 스캔이 같은 식으로 만들어져야 한다."""
    return {
        "rate": float(rate),
        "log_volume": math.log10(max(float(volume), 1.0)),
        "log_price": math.log10(max(float(price), 1.0)),
        "hour": t.hour + t.minute / 60.0,
        "weekday": float(t.weekday()),
        "rank": float(rank) if rank else 999.0,
        "n_scan": float(n_scan),
        "ob_imbalance": float(ob_imbalance) if ob_imbalance is not None else float("nan"),
        "index_chg": float(index_chg) if index_chg is not None else float("nan"),
        "rvol": float(rvol) if rvol is not None else float("nan"),
    }


def _minute_forward_price(mtl: dict, symbol: str, target: datetime) -> float | None:
    """분봉에서 target 시각 이후 첫 봉의 종가(생존편향 없는 forward price)."""
    series = mtl.get((symbol, target.strftime("%Y%m%d")))
    if not series:
        return None
    tt = target.strftime("%H%M%S")
    times = [x[0] for x in series]
    i = bisect_left(times, tt)
    return series[i][1] if i < len(series) else None


def _forward_price(timeline: list[tuple[datetime, float]],
                   target: datetime) -> float | None:
    """symbol 가격 타임라인에서 target 시각에 가장 가까운 가격(허용오차 내)."""
    if not timeline:
        return None
    times = [t for t, _ in timeline]
    i = bisect_left(times, target)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(timeline):
            dt = abs(timeline[j][0] - target)
            if dt <= _TOL and (best is None or dt < best[0]):
                best = (dt, timeline[j][1])
    return best[1] if best else None


def build_scan_dataset(store: DataStore, horizon_min: int = DEFAULT_HORIZON_MIN,
                       cost_bps: float = DEFAULT_COST_BPS) -> list[dict[str, Any]]:
    """surge_scan → forward-return 라벨 데이터셋(비용 차감한 순수익 라벨).

    forward price 는 분봉(생존편향 없음) 우선, 없으면 스캔 스냅샷으로 폴백.
    """
    obmap = store.orderbook_map()
    mtl = store.minute_timeline()
    rows = store.conn.execute(
        "SELECT ts,symbol,name,price,rate,volume,rank,index_chg,rvol "
        "FROM surge_scan ORDER BY ts"
    ).fetchall()
    recs = []
    for r in rows:
        try:
            t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if not r["price"] or r["price"] <= 0:
            continue
        recs.append({"t": t, "symbol": r["symbol"], "price": float(r["price"]),
                     "rate": float(r["rate"] or 0), "volume": float(r["volume"] or 0),
                     "rank": r["rank"], "index_chg": r["index_chg"],
                     "rvol": r["rvol"]})
    # symbol별 가격 타임라인 / ts별 동시급등 종목수
    timeline: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    nscan: dict[datetime, int] = defaultdict(int)
    for x in recs:
        timeline[x["symbol"]].append((x["t"], x["price"]))
        nscan[x["t"]] += 1
    for s in timeline:
        timeline[s].sort()

    horizon = timedelta(minutes=horizon_min)
    out: list[dict[str, Any]] = []
    for x in recs:
        target = x["t"] + horizon
        fp = _minute_forward_price(mtl, x["symbol"], target)   # 분봉 우선(편향 없음)
        src = "minute"
        if fp is None:
            fp = _forward_price(timeline[x["symbol"]], target)  # 폴백: 스캔 스냅샷
            src = "scan"
        if fp is None:
            continue  # N분 뒤 가격을 못 찾음 → 라벨 불가
        fwd = fp / x["price"] - 1.0 - cost_bps / 10000.0   # 왕복 비용 차감(정직)
        ts_str = x["t"].strftime("%Y-%m-%d %H:%M:%S")
        ob = obmap.get((ts_str, x["symbol"]))
        row = feat_row(x["rate"], x["volume"], x["price"], x["rank"],
                       nscan[x["t"]], x["t"], ob, x.get("index_chg"), x.get("rvol"))
        row.update(fwd_ret=fwd, symbol=x["symbol"], ts=ts_str, src=src)
        out.append(row)
    return out


def build_trade_dataset(store: DataStore) -> list[dict[str, Any]]:
    """청산 완료된 실제 매매 → 검증용(피처 + 실현 손익률·익절여부)."""
    rows = store.conn.execute(
        "SELECT * FROM surge_trade WHERE status='closed' AND entry_rate IS NOT NULL"
    ).fetchall()
    out = []
    for r in rows:
        try:
            t = datetime.strptime(r["entry_ts"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        row = feat_row(r["entry_rate"], r["entry_volume"] or 0, r["entry_price"],
                       r["entry_rank"], r["entry_nscan"] or 1, t, r["entry_ob_imbalance"],
                       rvol=(r["entry_rvol"] if "entry_rvol" in r.keys() else None))
        row.update(pnl_pct=(r["pnl_pct"] or 0) / 100.0,
                   win=1 if r["reason"] == "익절" else 0, name=r["name"])
        out.append(row)
    return out


def _ic(y_true: list[float], y_pred: list[float]) -> float:
    """예측-실제 순위상관 비슷한 단순 피어슨 상관(정보계수)."""
    import numpy as np
    if len(y_true) < 3:
        return float("nan")
    a, b = np.array(y_true), np.array(y_pred)
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _make_model(n: int):
    """표본 수에 맞는 모델. 소데이터는 선형(Ridge), 충분하면 얕은 LightGBM.
    (데이터 부족 시 트리는 과적합 → 단순 모델이 더 견고하다는 원칙.)"""
    if n >= LGBM_MIN:
        import lightgbm as lgb
        return lgb.LGBMRegressor(
            n_estimators=200, learning_rate=0.03, max_depth=3, num_leaves=7,
            min_child_samples=50, subsample=0.8, colsample_bytree=0.8,
            reg_lambda=1.0, verbose=-1), f"lightgbm(depth3, n={n})"
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    return Pipeline([("imp", SimpleImputer(strategy="median")),
                     ("sc", StandardScaler()),
                     ("ridge", Ridge(alpha=1.0))]), f"ridge(linear, n={n})"


def train(store: DataStore, horizon_min: int = DEFAULT_HORIZON_MIN,
          min_samples: int = MIN_SAMPLES, cost_bps: float = DEFAULT_COST_BPS,
          slippage_bps: float = DEFAULT_SLIPPAGE_BPS) -> dict[str, Any]:
    """스캔 데이터로 학습. 표본 부족 시 거부. 시간순 홀드아웃으로 OOS 점검.
    표본<3000 이면 선형(Ridge), 이상이면 얕은 LightGBM. 라벨엔 비용+슬리피지 차감."""
    import numpy as np

    total_cost = cost_bps + slippage_bps
    ds = build_scan_dataset(store, horizon_min, total_cost)
    if len(ds) < min_samples:
        return {"error": f"학습 데이터 부족: 라벨 {len(ds)}개 < 최소 {min_samples}개. "
                         f"봇을 더 돌려 데이터를 쌓으세요(과적합 방지 게이트)."}

    X = np.array([[r[f] for f in FEATURES] for r in ds], dtype=float)
    y = np.array([r["fwd_ret"] for r in ds], dtype=float)

    # 시간순 70/30 홀드아웃(미래누수 없이 OOS 신뢰성 확인)
    cut = int(len(ds) * 0.7)
    oos, _ = _make_model(cut)
    oos.fit(X[:cut], y[:cut])
    ic = _ic(list(y[cut:]), list(oos.predict(X[cut:]))) if len(ds) - cut >= 3 else float("nan")

    # 전체로 최종 학습·저장
    model, kind = _make_model(len(ds))
    model.fit(X, y)
    ob_cov = sum(1 for r in ds if r["ob_imbalance"] == r["ob_imbalance"]) / len(ds)
    min_cov = sum(1 for r in ds if r.get("src") == "minute") / len(ds)
    meta = {"rows": len(ds), "horizon_min": horizon_min, "kind": kind,
            "cost_bps": cost_bps, "slippage_bps": slippage_bps,
            "span": (ds[0]["ts"], ds[-1]["ts"]), "oos_ic": ic,
            "mean_fwd_net": float(y.mean()), "ob_coverage": ob_cov,
            "minute_coverage": min_cov}
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": model, "features": FEATURES, "meta": meta}, f)
    return meta


def load_model() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def score_candidates(bundle: dict, candidates: list[dict[str, Any]],
                     n_scan: int, t: datetime | None = None) -> None:
    """라이브 스캔 후보들에 ml_score(예측 N분 수익) 부여(in-place)."""
    import numpy as np
    t = t or datetime.now()
    model, feats = bundle["model"], bundle["features"]
    for c in candidates:
        f = feat_row(c["rate"], c["volume"], c["price"], c.get("rank"), n_scan, t,
                     c.get("ob_imbalance"), c.get("index_chg"), c.get("rvol"))
        X = np.array([[f[k] for k in feats]], dtype=float)
        c["ml_score"] = float(model.predict(X)[0])


def main() -> None:
    ap = argparse.ArgumentParser(description="급등주 단타 학습")
    ap.add_argument("--train", action="store_true", help="모델 학습·저장")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_MIN,
                    help="forward-return 지평(분)")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS,
                    help="왕복 거래비용(bp) — forward 라벨에서 차감")
    ap.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS,
                    help="슬리피지(bp) — 비용에 더해 차감(보수적)")
    args = ap.parse_args()

    store = DataStore()
    scan_ds = build_scan_dataset(store, args.horizon, args.cost_bps + args.slippage_bps)
    trade_ds = build_trade_dataset(store)
    n_scan_raw = store.conn.execute("SELECT COUNT(*) c FROM surge_scan").fetchone()["c"]
    n_days = store.conn.execute(
        "SELECT COUNT(DISTINCT substr(ts,1,10)) c FROM surge_scan").fetchone()["c"]

    n_min = sum(1 for r in scan_ds if r.get("src") == "minute")
    n_mbar = store.conn.execute("SELECT COUNT(*) c FROM minute_bar").fetchone()["c"]
    print("=== 단타 학습 데이터 현황 ===")
    print(f"  스캔 원천: {n_scan_raw:,}행 ({n_days}일치)")
    print(f"  분봉: {n_mbar:,}봉 저장")
    print(f"  라벨 가능(forward {args.horizon}분): {len(scan_ds):,}개 "
          f"(분봉기반 {n_min:,}·스캔폴백 {len(scan_ds)-n_min:,})")
    print(f"  실제 매매(검증용, 청산완료): {len(trade_ds)}건")
    print(f"  학습 게이트: 최소 {args.min_samples}개 필요")

    if args.train:
        print("\n모델 학습 중...", flush=True)
        meta = train(store, horizon_min=args.horizon, min_samples=args.min_samples,
                     cost_bps=args.cost_bps, slippage_bps=args.slippage_bps)
        if "error" in meta:
            print("  ⚠️ " + meta["error"])
        else:
            ic = meta["oos_ic"]
            ic_txt = f"{ic:+.3f}" if ic == ic else "N/A(표본부족)"
            print(f"  ✅ 저장: {MODEL_PATH} · 모델={meta['kind']}")
            print(f"     학습 {meta['rows']:,}행 · OOS IC {ic_txt} · "
                  f"평균 순수익(비용{meta['cost_bps']:.0f}+슬리피지{meta['slippage_bps']:.0f}bp차감) "
                  f"{meta['mean_fwd_net']*100:+.2f}% · 호가커버 {meta['ob_coverage']*100:.0f}%")
            print("     ※ IC가 0 근처면 아직 예측력 없음 — 데이터 더 필요")
    else:
        if len(scan_ds) < args.min_samples:
            print(f"\n아직 학습 불가. 봇(급등주_봇_시작.bat)을 장중에 더 돌려 데이터를 쌓으세요.")
        else:
            print(f"\n학습 가능. `python -m src.surge_ml --train` 실행.")
    store.close()


if __name__ == "__main__":
    main()
