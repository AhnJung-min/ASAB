"""급등주 청산(exit) 정책 분석 — 분봉으로 '어떤 청산이 최선이었나'를 데이터로 검증.

진입은 고정(스캔에 처음 뜬 시점·가격)하고, 청산 규칙만 바꿔가며 순수익을 비교한다.
모델/학습 없이, 게이트 기다릴 필요 없이 당장 수익을 개선할 수 있는 그라운드 분석.
일봉 트랙 tune_exits.py 의 단타판.

방법:
  진입 = surge_scan 에서 각 (종목,날짜)가 필터(등락률/거래량/가격/비레버리지)를
         처음 통과한 시점. 진입가 = 그때 스캔가.
  청산 = 그 시점 이후 분봉을 따라가며 익절/손절/트레일링/장마감 중 먼저 닿는 것.
  순수익 = 청산수익 - 왕복비용. 같은 진입에 청산 격자를 스윕해 공정 비교.

⚠️ 분봉이 있는 종목만 분석된다 → 분봉_수집.bat 을 매일 돌려 표본을 쌓아야 한다.
   표본(N)이 적으면 결과는 노이즈다. N 을 보고 강한 결론은 금물.

  python -m src.surge_exits
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

from .data.store import DataStore
from .surge_bot import is_leverage_inverse
from .surge_ml import DEFAULT_COST_BPS


def _cfg_surge() -> dict:
    try:
        raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        return (raw.get("surge") or {})
    except OSError:
        return {}


def build_entries(store: DataStore) -> list[dict[str, Any]]:
    """각 (종목,날짜)의 '필터 첫 통과' 시점을 진입으로. 분봉 있는 것만."""
    s = _cfg_surge()
    min_rate = float(s.get("min_rate", 5.0)); max_rate = float(s.get("max_rate", 20.0))
    min_vol = int(s.get("min_volume", 100_000))
    pmin = int(s.get("price_min", 1_000)); pmax = int(s.get("price_max", 100_000))
    excl_lev = bool(s.get("exclude_leverage_inverse", True))

    mtl = store.minute_timeline()                 # (symbol, yyyymmdd) -> [(time, close)]
    rows = store.conn.execute(
        "SELECT ts,symbol,name,price,rate,volume FROM surge_scan ORDER BY ts").fetchall()
    seen: set[tuple[str, str]] = set()
    entries = []
    for r in rows:
        date = r["ts"][:10].replace("-", "")
        key = (r["symbol"], date)
        if key in seen:
            continue
        if not (min_rate <= (r["rate"] or 0) <= max_rate):
            continue
        if (r["volume"] or 0) < min_vol or not (pmin <= (r["price"] or 0) <= pmax):
            continue
        if excl_lev and is_leverage_inverse(r["name"] or ""):
            continue
        if (r["symbol"], date) not in mtl:
            continue  # 분봉 없으면 시뮬 불가
        seen.add(key)
        entries.append({"symbol": r["symbol"], "name": r["name"], "date": date,
                        "time": r["ts"][11:].replace(":", ""), "price": float(r["price"])})
    return entries


def simulate(series: list[tuple[str, float, float, float]], entry_price: float,
             entry_time: str, tp: float, sl: float, trail: float,
             cost_bps: float) -> float | None:
    """청산수익(순, 비용차감) 반환. series=[(time, high, low, close)] 시간순.

    한 봉 내 동시충족 시 손절>익절>트레일링 순(보수적=과대평가 방지).
    """
    if entry_price <= 0:
        return None
    peak = entry_price
    last_close = None
    triggered = None
    for t, hi, lo, cl in series:
        if t < entry_time:
            continue
        last_close = cl
        if sl > 0 and lo <= entry_price * (1 - sl / 100):
            triggered = -sl / 100; break
        if tp > 0 and hi >= entry_price * (1 + tp / 100):
            triggered = tp / 100; break
        peak = max(peak, hi)
        if trail > 0 and peak > entry_price and lo <= peak * (1 - trail / 100):
            triggered = peak * (1 - trail / 100) / entry_price - 1; break
    if last_close is None:
        return None  # 진입시각 이후 분봉 없음
    ret = triggered if triggered is not None else (last_close / entry_price - 1)
    return ret - cost_bps / 10000.0


def main() -> None:
    ap = argparse.ArgumentParser(description="급등주 청산 정책 분석")
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    args = ap.parse_args()

    store = DataStore()
    # 분봉 series 를 (symbol,date) -> [(time, high, low, close)] 로 준비
    bars: dict[tuple[str, str], list] = {}
    for r in store.conn.execute(
            "SELECT symbol,date,time,high,low,close FROM minute_bar ORDER BY symbol,date,time"):
        bars.setdefault((r["symbol"], r["date"]), []).append(
            (r["time"], r["high"], r["low"], r["close"]))

    entries = [e for e in build_entries(store) if (e["symbol"], e["date"]) in bars]
    n = len(entries)
    print(f"=== 급등주 청산 분석 · 진입표본 N={n} (분봉 보유 종목만) ===")
    if n == 0:
        print("분봉 데이터가 없습니다. 먼저 분봉_수집.bat 을 장 마감 후 실행하세요.")
        store.close(); return
    if n < 50:
        print(f"⚠️ 표본 {n}개는 너무 적어 결과가 노이즈입니다. 며칠 더 분봉을 모으세요.")

    # 청산 격자
    tps = [2, 3, 5, 8, 0]        # 0 = 익절 안 함
    sls = [1, 2, 3, 5, 0]        # 0 = 손절 안 함
    trails = [0, 2, 3]           # 0 = 트레일링 안 함

    results = []
    for tp in tps:
        for sl in sls:
            for tr in trails:
                rets = [r for e in entries
                        if (r := simulate(bars[(e["symbol"], e["date"])], e["price"],
                                          e["time"], tp, sl, tr, args.cost_bps)) is not None]
                if not rets:
                    continue
                mean = sum(rets) / len(rets)
                win = sum(1 for x in rets if x > 0) / len(rets)
                results.append({"tp": tp, "sl": sl, "tr": tr, "mean": mean,
                                "win": win, "n": len(rets)})

    # 기준선: 청산 없이 장마감까지 보유
    hold = [r for e in entries
            if (r := simulate(bars[(e["symbol"], e["date"])], e["price"],
                              e["time"], 0, 0, 0, args.cost_bps)) is not None]
    hold_mean = sum(hold) / len(hold) if hold else 0.0

    results.sort(key=lambda d: -d["mean"])
    cur = next((r for r in results if r["tp"] == 3 and r["sl"] == 2 and r["tr"] == 2), None)

    print(f"\n기준선(청산없이 장마감 보유): 평균 순수익 {hold_mean*100:+.2f}%")
    print(f"\n{'익절':>4}{'손절':>5}{'트레일':>6}{'평균순수익':>11}{'승률':>7}{'N':>6}")
    print("-" * 40)
    for r in results[:12]:
        tp = f"{r['tp']}%" if r["tp"] else "off"
        sl = f"{r['sl']}%" if r["sl"] else "off"
        tr = f"{r['tr']}%" if r["tr"] else "off"
        print(f"{tp:>4}{sl:>5}{tr:>6}{r['mean']*100:>10.2f}%{r['win']*100:>6.0f}%{r['n']:>6}")
    if cur:
        print(f"\n현재 설정(익절3/손절2/트레일2): 평균 {cur['mean']*100:+.2f}% · "
              f"승률 {cur['win']*100:.0f}% · N={cur['n']}")
    print("\n해석: 상위 조합이 '현재 설정'·'기준선'보다 일관되게(여러 날·큰 N) 높아야 의미.")
    print("      N 작으면 1~2개 대박/쪽박에 휘둘린 노이즈 — 분봉 더 쌓고 재확인.")
    store.close()


if __name__ == "__main__":
    main()
