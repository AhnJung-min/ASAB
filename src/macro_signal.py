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


def _eval(sig: pd.DataFrame, ret: pd.DataFrame, topk: int) -> dict:
    """sig[t] (이미 시차적용됨) 로 ret[t]를 설명. 횡단면 IC + 롱숏."""
    cols = [c for c in sig.columns if c in ret.columns]
    sig, ret = sig[cols], ret[cols]
    idx = sig.dropna(how="all").index.intersection(ret.dropna(how="all").index)
    ics, ls = [], []
    for t in idx:
        s, r = sig.loc[t], ret.loc[t]
        ok = s.notna() & r.notna()
        if ok.sum() < 4:
            continue
        s, r = s[ok], r[ok]
        ics.append(s.rank().corr(r.rank()))            # 스피어만 IC
        order = s.sort_values(ascending=False).index
        top = r[order[:topk]].mean()
        bot = r[order[-topk:]].mean()
        ls.append(top - bot)
    ls = pd.Series(ls)
    eq = (1 + ls).cumprod()
    return {
        "n": len(ls),
        "ic": float(np.nanmean(ics)) if ics else float("nan"),
        "ls_mean_m": ls.mean(), "ls_ann": ls.mean() * 12,
        "hit": (ls > 0).mean(), "ls_cum": eq.iloc[-1] - 1 if len(eq) else float("nan"),
    }


def run(topk: int = 3) -> dict:
    store = DataStore()
    ret = sector_monthly_return(store)
    sig = export_yoy(store)
    store.close()
    # 발표시차 반영: export[t-1] → return[t]  (no-lookahead)
    lagged = sig.shift(1)
    # 비교용 누수 버전: export[t] → return[t] (동월, 발표 전 정보 사용 = 미래누수)
    return {"lagged": _eval(lagged, ret, topk), "leak": _eval(sig, ret, topk),
            "sectors": [c for c in sig.columns if c in ret.columns]}


def main() -> None:
    res = run()
    print("C — 수출 섹터 틸트 검증 (횡단면, 상위3 vs 하위3)")
    print(f"섹터 {len(res['sectors'])}개: {', '.join(res['sectors'])}\n")
    print(f"{'':16}{'IC':>8}{'롱숏(월)':>10}{'연환산':>9}{'승률':>8}{'누적':>9}{'표본':>6}")
    for key, label in (("lagged", "발표시차반영(정직)"), ("leak", "동월(누수참고)")):
        m = res[key]
        print(f"{label:16}{m['ic']:>8.3f}{m['ls_mean_m']*100:>9.2f}%{m['ls_ann']*100:>8.1f}%"
              f"{m['hit']*100:>7.0f}%{m['ls_cum']*100:>8.0f}%{m['n']:>6}")
    print("\n해석: IC>0 또는 롱숏 연환산>0 이면 '수출 강한 섹터가 다음 달 우위'.")
    print("      발표시차반영이 진짜 실력. 동월(누수)이 크게 좋으면 시차효과가 큰 것.")


if __name__ == "__main__":
    main()
