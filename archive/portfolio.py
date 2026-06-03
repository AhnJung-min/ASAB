"""포트폴리오 자산/손익 계산 (원화 기준).

모의 서버의 '주문가능 외화' 같은 값은 신뢰도가 낮아(통합증거금 환경에서
실제 원화 한도와 불일치) 사용하지 않는다. 대신 다음 항등식으로 직접 계산한다:

    총자산 = 시작자본 + 실현손익 + 평가손익
    현금   = 시작자본 + 실현손익 - 보유종목 매입원가

  - 시작자본: config.initial_capital_krw (예: 10,000,000원)
  - 실현손익: 청산 완료된 매매(surge_trade)의 손익 합 (USD -> 원화 환산)
  - 평가손익: 현재 보유종목의 평가손익 (USD -> 원화 환산)
모두 원화로 환산해 일관되게 보여준다.
"""
from __future__ import annotations

from typing import Any

from .data.store import DataStore


def equity_snapshot(
    store: DataStore,
    holdings: list[dict[str, Any]],
    fx_rate: float,
    initial_capital_krw: float,
) -> dict[str, float]:
    # 실현손익(USD) = 청산된 거래 손익 합
    row = store.conn.execute(
        "SELECT COALESCE(SUM(pnl),0) AS s FROM surge_trade WHERE status='closed'"
    ).fetchone()
    realized_usd = float(row["s"] or 0.0)

    unrealized_usd = sum(h.get("pnl", 0.0) for h in holdings)
    invested_usd = sum(h.get("buy_usd", 0.0) for h in holdings)
    holdings_value_usd = sum(h.get("eval_usd", 0.0) for h in holdings)

    realized_krw = realized_usd * fx_rate
    unrealized_krw = unrealized_usd * fx_rate
    holdings_krw = holdings_value_usd * fx_rate
    cash_krw = initial_capital_krw + realized_krw - invested_usd * fx_rate
    total_krw = cash_krw + holdings_krw  # = 시작자본 + 실현 + 평가

    return {
        "initial_krw": float(initial_capital_krw),
        "cash_krw": cash_krw,
        "holdings_krw": holdings_krw,
        "total_krw": total_krw,
        "realized_krw": realized_krw,
        "unrealized_krw": unrealized_krw,
        "total_pnl_krw": realized_krw + unrealized_krw,
        "return_pct": (total_krw / initial_capital_krw - 1) * 100 if initial_capital_krw else 0.0,
        "fx_rate": fx_rate,
    }
