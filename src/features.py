"""ML 피처 엔지니어링.

저장된 일봉(OHLCV)에서 '시점(as-of) 기준' 피처 행렬과 미래수익률 라벨을 만든다.
미래정보 누출 방지: 피처는 t시점까지의 데이터만 사용, 라벨은 t→t+hold 미래수익률
(학습 타깃으로만 사용). 워크포워드에서 train_date < test_date 로 시간 분리한다.

반환 컬럼: date, symbol, [피처들...], fwd_ret(라벨)
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from .data.store import DataStore

BENCHMARK_SYMBOL = "069500"  # KODEX200 (상대강도 기준)
WARMUP = 130                  # 피처 계산에 필요한 최소 일봉(252 고가거리 위해 넉넉히)

FEATURES = [
    "ret_5", "ret_20", "ret_60", "ret_120", "ret_60_skip5",
    "ma20_ratio", "ma60_ratio", "vol_20", "vol_60", "rsi_14",
    "rel_str_60", "liq", "vol_surge", "high_252_dist",
]


def _rsi(closes: list[int], n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        (gains if ch >= 0 else losses).append(abs(ch))
    ag = sum(gains) / n
    al = sum(losses) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def compute_features(closes, values, volumes, idx_ret60) -> dict | None:
    """as-of 피처. closes/values/volumes 는 t시점까지(과거→t) 리스트."""
    n = len(closes)
    if n < WARMUP:
        return None
    c = closes
    rets = [c[i] / c[i - 1] - 1 for i in range(max(1, n - 61), n)]

    def mom(k):
        return c[-1] / c[-1 - k] - 1 if n > k else 0.0

    ret_60 = mom(60)
    feat = {
        "ret_5": mom(5),
        "ret_20": mom(20),
        "ret_60": ret_60,
        "ret_120": mom(120),
        "ret_60_skip5": (c[-6] / c[-61] - 1) if n >= 61 else 0.0,
        "ma20_ratio": c[-1] / (sum(c[-20:]) / 20) - 1,
        "ma60_ratio": c[-1] / (sum(c[-60:]) / 60) - 1,
        "vol_20": statistics.pstdev(rets[-20:]) if len(rets) >= 20 else 0.0,
        "vol_60": statistics.pstdev(rets) if len(rets) > 1 else 0.0,
        "rsi_14": _rsi(c, 14),
        "rel_str_60": ret_60 - (idx_ret60 if idx_ret60 is not None else 0.0),
        "liq": math.log10(max(sum(values[-20:]) / 20, 1)),
        "vol_surge": (sum(volumes[-5:]) / 5) / max(sum(volumes[-60:]) / 60, 1),
        "high_252_dist": c[-1] / max(c[-252:]) - 1,
    }
    return feat


def build_dataset(store: DataStore, hold_days: int = 20, max_pool: int = 300,
                  start_date: str = "20160101") -> list[dict[str, Any]]:
    """패널 데이터셋 생성: (종목 × 리밸런스일) 행, 피처 + fwd_ret 라벨."""
    # 유동성 상위 max_pool 개별주
    master = store.master_symbols(by_liquidity=True)
    pool = [m["symbol"] for m in master][: max_pool * 2]  # 여유있게

    series = {}
    for sym in pool:
        rows = store.get_daily(sym)
        if len(rows) >= WARMUP:
            series[sym] = rows
        if len(series) >= max_pool:
            break

    # 인덱스(벤치마크)
    idx_rows = store.get_daily(BENCHMARK_SYMBOL)
    idx_close = [r["close"] for r in idx_rows]
    idx_dates = [r["date"] for r in idx_rows]
    import bisect
    def idx_ret60_asof(t):
        pos = bisect.bisect_right(idx_dates, t) - 1
        if pos < 60:
            return None
        return idx_close[pos] / idx_close[pos - 60] - 1

    # 종목별 (날짜→인덱스), 배열
    pos_by = {s: {r["date"]: i for i, r in enumerate(rows)} for s, rows in series.items()}
    arrs = {s: ([r["close"] for r in rows], [r["value"] for r in rows],
                [r["volume"] for r in rows], [r["date"] for r in rows])
            for s, rows in series.items()}

    # 리밸런스 날짜 = 인덱스 거래일을 hold_days 간격으로
    cal = [d for d in idx_dates if d >= start_date]
    rebal = cal[::hold_days]

    data = []
    for t in rebal:
        ir = idx_ret60_asof(t)
        for s in series:
            p = pos_by[s].get(t)
            if p is None or p < WARMUP - 1:
                continue
            closes, values, volumes, dates = arrs[s]
            if p + hold_days >= len(closes):
                continue  # 미래수익률(라벨) 없음
            f = compute_features(closes[: p + 1], values[: p + 1], volumes[: p + 1], ir)
            if f is None:
                continue
            f["date"] = t
            f["symbol"] = s
            f["fwd_ret"] = closes[p + hold_days] / closes[p] - 1
            data.append(f)
    return data


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    store = DataStore()
    ds = build_dataset(store)
    store.close()
    print(f"데이터셋: {len(ds):,}행 · 피처 {len(FEATURES)}개")
    if ds:
        import statistics as st
        print(f"기간: {ds[0]['date']} ~ {ds[-1]['date']}")
        print(f"라벨(fwd_ret) 평균 {st.mean(d['fwd_ret'] for d in ds)*100:.2f}% "
              f"표준편차 {st.pstdev(d['fwd_ret'] for d in ds)*100:.2f}%")


if __name__ == "__main__":
    main()
