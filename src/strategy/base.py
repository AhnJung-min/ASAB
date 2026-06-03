"""전략 인터페이스. 가격 시계열을 받아 매매 신호를 낸다."""
from __future__ import annotations

from enum import Enum


class Signal(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Strategy:
    """모든 전략의 베이스. update() 에 종목별 최신가를 넣고 신호를 받는다."""

    def update(self, symbol: str, price: float) -> Signal:  # noqa: ARG002
        raise NotImplementedError
