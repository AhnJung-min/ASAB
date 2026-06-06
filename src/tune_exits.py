"""청산(손절/트레일링) 파라미터 스윕 — '순진한 손절의 함정' 회피용 탐색.

-8% 하드스톱이 휩쏘로 수익을 망쳤던 문제를 정량 비교한다.
공정한 A/B를 위해 **모든 변형과 기준선을 동일한 종목별 회계**로 돌린다
(무청산 기준선 = stop_loss_pct=999, 발동되지 않는 손절).

유동성 유니버스를 고정하고 series 를 1회만 로드해 여러 설정을 빠르게 비교한다.

실행:  python -m src.tune_exits --top-n 5 --max-pool 200
       python -m src.tune_exits --regime-filter
"""
from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .backtest import _load_series, run_backtest
from .data.store import DataStore

# 탐색 그리드(작게 유지 — 그리드 과적합 방지)
HARD_STOPS = [8, 12, 15, 20, 25]      # 하드 손절 %
TRAILS = [10, 15, 20, 25]             # 트레일링 %
BASELINE_STOP = 999.0                 # 발동 안 되는 손절 = 무청산 매칭 기준선


def _run(store, series, *, stop=0.0, trail=0.0, **kw) -> dict:
    res = run_backtest(store, series=series, stop_loss_pct=stop, trailing_pct=trail, **kw)
    if "error" in res:
        return {"error": res["error"]}
    m = res["metrics"]
    return {"total": m["total_return"], "cagr": m["cagr"],
            "sharpe": m["sharpe"], "mdd": m["mdd"]}


def main() -> None:
    ap = argparse.ArgumentParser(description="청산 파라미터 스윕")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--max-pool", type=int, default=200, help="유동성 상위 N(현실적 유니버스)")
    ap.add_argument("--hold-days", type=int, default=20)
    ap.add_argument("--regime-filter", action="store_true")
    args = ap.parse_args()

    store = DataStore()
    print("데이터 로딩...", flush=True)
    series = _load_series(store)
    common = dict(top_n=args.top_n, hold_days=args.hold_days,
                  max_pool=args.max_pool, regime_filter=args.regime_filter)

    print(f"유니버스 상위{args.max_pool} · top{args.top_n} · 보유{args.hold_days}일 · "
          f"국면필터={'ON' if args.regime_filter else 'OFF'}\n계산 중...", flush=True)

    # 기준선(무청산, 종목별 회계)
    base = _run(store, series, stop=BASELINE_STOP, **common)
    if "error" in base:
        print(base["error"]); store.close(); return

    rows = [("무청산(기준선)", base)]
    for sl in HARD_STOPS:
        rows.append((f"손절 {sl}%", _run(store, series, stop=float(sl), **common)))
    for tr in TRAILS:
        rows.append((f"트레일링 {tr}%", _run(store, series, trail=float(tr), **common)))
    store.close()

    bs, bm = base["sharpe"], base["mdd"]
    print(f"\n{'설정':<16}{'누적':>9}{'CAGR':>8}{'샤프':>7}{'MDD':>8}"
          f"{'샤프Δ':>8}{'MDDΔ':>8}")
    print("-" * 66)
    for label, r in rows:
        if "error" in r:
            print(f"{label:<16}  (오류)")
            continue
        d_sh = r["sharpe"] - bs
        d_mdd = (r["mdd"] - bm) * 100   # 양수면 낙폭 개선(덜 음수)
        print(f"{label:<16}{r['total']*100:>8.1f}%{r['cagr']*100:>7.1f}%"
              f"{r['sharpe']:>7.2f}{r['mdd']*100:>7.1f}%{d_sh:>+8.2f}{d_mdd:>+7.1f}%")

    print("\n해석: 샤프Δ>0 이면서 MDDΔ>0(낙폭 축소) = 청산이 위험효율을 개선.")
    print("      샤프Δ<0 이면 청산이 수익을 깎는 것(휩쏘). 기준선과 같은 회계라 공정 비교.")


if __name__ == "__main__":
    main()
