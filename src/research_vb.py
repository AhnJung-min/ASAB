"""변동성 돌파(래리 윌리엄스 K-돌파) 전략 정직 검증 — 연구 전용(라이브 미연결).

국내 자동매매 봇(크립토→주식 이식)에서 사실상 표준인 규칙:
  진입: 당일 고가가 [당일 시가 + K x (전일 고가-전일 저가)] 돌파 시 그 가격에 매수
  청산: 익일 시가 매도 (오버나이트 1일 보유)
  K   : 통상 0.5 (0.3~0.7 스윕)

검증 설계(이 프로젝트의 비용 모델과 동일 철학):
  - 진입가 = 돌파가격 x (1+슬리피지), 청산가 = 익일시가 x (1-슬리피지)
  - 왕복 비용 cost_bps 차감(수수료+거래세)
  - 갭상승으로 시가가 이미 돌파가 위면 시가 진입(현실 보수 반영)
  - 유니버스: 유동성 상위 개별주 + 지수 ETF(069500/229200) 별도 표기

실행:
  python -m src.research_vb                 # K 스윕 + 종목군별 요약
  python -m src.research_vb --k 0.5 --top 20
"""
from __future__ import annotations

import argparse
import sys
from statistics import mean, stdev

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore

COST_BPS = 25.0      # 수수료+거래세(왕복)
SLIP_BPS = 10.0      # 체결 슬리피지(편도)


def load_ohlc(store: DataStore, symbol: str) -> list[dict]:
    rows = store.conn.execute(
        "SELECT date, open, high, low, close FROM daily_price "
        "WHERE symbol=? AND open>0 AND high>0 AND low>0 ORDER BY date", (symbol,)
    ).fetchall()
    return [dict(r) for r in rows]


def vb_returns(rows: list[dict], k: float) -> list[tuple[str, float]]:
    """일별 (날짜, 순수익률%) — 트리거된 날만. 익일 시가 청산."""
    out: list[tuple[str, float]] = []
    cost = COST_BPS / 1e4
    slip = SLIP_BPS / 1e4
    for i in range(1, len(rows) - 1):
        prev, cur, nxt = rows[i - 1], rows[i], rows[i + 1]
        rng = prev["high"] - prev["low"]
        if rng <= 0:
            continue
        target = cur["open"] + k * rng
        if cur["high"] < target:
            continue
        entry = max(target, cur["open"]) * (1 + slip)  # 갭상승이면 시가 진입
        exit_px = nxt["open"] * (1 - slip)
        if entry <= 0 or exit_px <= 0:
            continue
        out.append((cur["date"], (exit_px / entry - 1 - cost) * 100))
    return out


def summarize(name: str, trades: list[tuple[str, float]]) -> dict | None:
    if len(trades) < 30:
        return None
    rets = [r for _, r in trades]
    avg = mean(rets)
    sd = stdev(rets) if len(rets) > 1 else 0.0
    win = sum(1 for r in rets if r > 0) / len(rets) * 100
    # 누적(복리)
    eq = 1.0
    peak, mdd = 1.0, 0.0
    for r in rets:
        eq *= 1 + r / 100
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    # 거래일 단위 샤프(트리거된 날만이라 근사치) — 연환산은 트리거 빈도로
    years = max((int(trades[-1][0][:4]) - int(trades[0][0][:4])), 1)
    per_year = len(rets) / years
    sharpe = (avg / sd * (per_year ** 0.5)) if sd > 0 else 0.0
    return {"name": name, "n": len(rets), "avg": avg, "win": win,
            "cum": (eq - 1) * 100, "mdd": mdd * 100, "sharpe": sharpe}


def main() -> None:
    ap = argparse.ArgumentParser(description="변동성 돌파(K) 전략 검증")
    ap.add_argument("--k", type=float, default=0.0, help="단일 K만 검증(0=스윕)")
    ap.add_argument("--top", type=int, default=20, help="유동성 상위 개별주 수")
    args = ap.parse_args()

    store = DataStore()
    masters = store.master_symbols(by_liquidity=True)[: args.top]
    groups: list[tuple[str, list[str]]] = [
        ("KODEX200(069500)", ["069500"]),
        ("KODEX코스닥150(229200)", ["229200"]),
        (f"개별주 유동성 top{args.top}", [m["symbol"] for m in masters]),
    ]
    ks = [args.k] if args.k > 0 else [0.3, 0.5, 0.7]

    print(f"비용: 왕복 {COST_BPS:.0f}bps + 슬리피지 편도 {SLIP_BPS:.0f}bps / 청산: 익일 시가")
    print(f"{'그룹':<28}{'K':>4}{'거래':>7}{'평균%':>8}{'승률%':>7}{'누적%':>10}{'MDD%':>8}{'샤프':>7}")
    for gname, syms in groups:
        series = {s: load_ohlc(store, s) for s in syms}
        for k in ks:
            allt: list[tuple[str, float]] = []
            for s in syms:
                allt.extend(vb_returns(series[s], k))
            allt.sort()
            r = summarize(gname, allt)
            if r is None:
                print(f"{gname:<28}{k:>4.1f}  표본 부족")
                continue
            print(f"{gname:<28}{k:>4.1f}{r['n']:>7}{r['avg']:>8.3f}{r['win']:>7.1f}"
                  f"{r['cum']:>10.1f}{r['mdd']:>8.1f}{r['sharpe']:>7.2f}")
    store.close()


if __name__ == "__main__":
    main()
