"""C — 수출 섹터 틸트 신호 검증 (횡단면, 발표시차 반영).

질문: "지난달 수출이 강했던 섹터의 주식이 이번 달 더 오르는가?"

핵심 방법론(누수 방지):
  - 발표시차: M월 수출은 익월(M+1) 1일 발표 → M+1월 포트폴리오 결정에만 사용.
    즉 signal = export_yoy[t-1], outcome = 바스켓수익[t] (sig.shift(1)).
  - 횡단면: 매월 섹터를 수출 YoY로 순위 → 상위 vs 하위 바스켓 수익 비교.
    시장 전체 추세(반도체 슈퍼사이클)를 상쇄해 교란을 줄인다.

지표: IC(랭크상관, 월평균), 롱숏(상위-하위) 월수익·연환산·승률·누적.
비교용으로 '발표시차 무시(누수)' 버전도 함께 출력해 시차의 영향을 드러낸다.

사용:  python -m src.macro_signal              (DB의 export_history + 일봉 사용)
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

from .data.store import DataStore

# export_history 섹터명 → 대표주 바스켓(코드). 컴퓨터/휴대폰은 반도체·삼성과 중복이라 제외.
SECTOR_BASKET = {
    "반도체": ["005930", "000660"],
    "자동차": ["005380", "000270"],
    "자동차부품": ["012330", "161390"],
    "선박": ["329180", "042660", "010140"],
    "철강": ["005490", "004020"],
    "이차전지": ["373220", "006400"],
    "석유제품": ["096770", "010950"],
    "바이오": ["207940", "068270"],
    "화장품": ["090430", "051900", "192820", "161890"],
}


def _monthly_close(store: DataStore, symbol: str) -> pd.Series:
    rows = store.get_daily(symbol)
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({r["date"][:6]: r["close"] for r in rows})  # YYYYMM(마지막값=월말)
    s.index = pd.to_datetime(s.index, format="%Y%m").to_period("M")
    return s


def sector_monthly_return(store: DataStore) -> pd.DataFrame:
    """섹터별 월수익률(동일비중 바스켓). index=월(Period), cols=섹터."""
    out = {}
    for sec, syms in SECTOR_BASKET.items():
        rets = []
        for sym in syms:
            c = _monthly_close(store, sym)
            if len(c) > 1:
                rets.append(c.pct_change())
        if rets:
            out[sec] = pd.concat(rets, axis=1).mean(axis=1)  # 종목 평균(결측 무시)
    return pd.DataFrame(out)


def export_yoy(store: DataStore) -> pd.DataFrame:
    """섹터별 수출 YoY(12개월). index=월(Period), cols=섹터."""
    rows = [dict(r) for r in store.get_export_history()]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    piv = df.pivot(index="month", columns="sector", values="export_kusd")
    piv.index = pd.PeriodIndex(piv.index, freq="M")
    return piv.sort_index().pct_change(12)


def _series(sig: pd.DataFrame, ret: pd.DataFrame, topk: int, cost_bps: float):
    """월별 IC·롱숏·롱온리초과(거래비용 반영) 시계열을 만든다.

      롱숏     = 상위K 평균 - 하위K 평균 (신호 품질 측정용; 공매도 가정)
      롱온리초과 = 상위K 평균 - 전체섹터 평균 (실제 구현 가능; 고수출 섹터 비중확대)
    비용: 직전월 대비 바뀐 종목 비중 × cost (라운드트립). 벤치마크(전체)는 회전 0.
    """
    cost = cost_bps / 10_000.0
    cols = [c for c in sig.columns if c in ret.columns]
    sig, ret = sig[cols], ret[cols]
    idx = sig.dropna(how="all").index.intersection(ret.dropna(how="all").index)
    recs = []
    prev_top: set[str] = set()
    prev_bot: set[str] = set()
    for t in sorted(idx):
        s, r = sig.loc[t], ret.loc[t]
        ok = s.notna() & r.notna()
        if ok.sum() < 4:
            continue
        s, r = s[ok], r[ok]
        order = list(s.sort_values(ascending=False).index)
        top, bot = set(order[:topk]), set(order[-topk:])
        ret_top, ret_bot, ret_all = r[list(top)].mean(), r[list(bot)].mean(), r.mean()
        to_top = len(top ^ prev_top) / max(len(top), 1)   # 회전율(대칭차/보유수)
        to_bot = len(bot ^ prev_bot) / max(len(bot), 1)
        recs.append({
            "month": t,
            "ic": s.rank().corr(r.rank()),
            "ls": (ret_top - ret_bot) - cost * (to_top + to_bot),
            "lo": (ret_top - ret_all) - cost * to_top,   # 롱온리 초과(순)
        })
        prev_top, prev_bot = top, bot
    return pd.DataFrame(recs).set_index("month") if recs else pd.DataFrame()


def _metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0}
    def stat(col):
        x = df[col].dropna()
        eq = (1 + x).cumprod()
        return {"mean_ann": x.mean() * 12, "hit": (x > 0).mean(),
                "cum": eq.iloc[-1] - 1 if len(eq) else float("nan")}
    return {"n": len(df), "ic": df["ic"].mean(), "ls": stat("ls"), "lo": stat("lo")}


def run(topk: int = 3, cost_bps: float = 30.0) -> dict:
    store = DataStore()
    ret = sector_monthly_return(store)
    sig = export_yoy(store)
    store.close()
    lagged = _series(sig.shift(1), ret, topk, cost_bps)   # 발표시차(정직)
    leak = _series(sig, ret, topk, cost_bps)              # 동월(누수 참고)
    half = len(lagged) // 2
    return {
        "sectors": [c for c in sig.columns if c in ret.columns],
        "cost_bps": cost_bps,
        "lagged": _metrics(lagged), "leak": _metrics(leak),
        "first_half": _metrics(lagged.iloc[:half]),
        "second_half": _metrics(lagged.iloc[half:]),
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="C — 수출 섹터 틸트 검증")
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--cost-bps", type=float, default=30.0, help="라운드트립 거래비용(bps)")
    args = ap.parse_args()

    res = run(topk=args.topk, cost_bps=args.cost_bps)
    print(f"C — 수출 섹터 틸트 검증 (상위{args.topk}, 거래비용 {args.cost_bps:.0f}bps 반영)")
    print(f"섹터 {len(res['sectors'])}개: {', '.join(res['sectors'])}\n")

    def line(label, m):
        if m["n"] == 0:
            print(f"{label:18} (표본 없음)"); return
        ls, lo = m["ls"], m["lo"]
        print(f"{label:18}{m['ic']:>7.3f}{ls['mean_ann']*100:>9.1f}%{ls['hit']*100:>6.0f}%"
              f"{lo['mean_ann']*100:>10.1f}%{lo['hit']*100:>6.0f}%{m['n']:>6}")

    print(f"{'':18}{'IC':>7}{'롱숏年':>9}{'승률':>6}{'롱온리초과年':>10}{'승률':>6}{'표본':>6}")
    print("-" * 62)
    line("발표시차(정직)", res["lagged"])
    line("동월(누수참고)", res["leak"])
    print("-" * 62)
    line(" └ 전반기", res["first_half"])
    line(" └ 후반기", res["second_half"])

    print("\n해석:")
    print("  · 롱숏=상위K-하위K(신호품질, 공매도가정) / 롱온리초과=상위K-전체평균(실구현 가능)")
    print("  · 거래비용 반영 後에도 롱온리초과 年>0 이면 '실제로 써먹을 만한' 섹터 틸트.")
    print("  · 전·후반 모두 양(+)이면 안정적, 한쪽만이면 기간의존(주의).")


if __name__ == "__main__":
    main()
