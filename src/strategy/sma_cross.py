"""단순이동평균(SMA) 골든/데드크로스 전략.

단기 이동평균이 장기 이동평균을 상향 돌파 -> 매수
하향 돌파 -> 매도
"""
from __future__ import annotations

from collections import defaultdict, deque

from .base import Signal, Strategy


class SmaCrossStrategy(Strategy):
    def __init__(self, short_window: int = 5, long_window: int = 20):
        if short_window >= long_window:
            raise ValueError("short_window 는 long_window 보다 작아야 합니다.")
        self.short_window = short_window
        self.long_window = long_window
        self._prices: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=long_window)
        )
        # 직전 단기-장기 관계(단기가 위였는지) 저장
        self._prev_short_above: dict[str, bool | None] = defaultdict(lambda: None)

    @staticmethod
    def _sma(values: deque[float], window: int) -> float:
        last = list(values)[-window:]
        return sum(last) / len(last)

    def update(self, symbol: str, price: float) -> Signal:
        prices = self._prices[symbol]
        prices.append(price)

        if len(prices) < self.long_window:
            return Signal.HOLD  # 데이터 부족

        short_ma = self._sma(prices, self.short_window)
        long_ma = self._sma(prices, self.long_window)
        short_above = short_ma > long_ma

        prev = self._prev_short_above[symbol]
        self._prev_short_above[symbol] = short_above

        if prev is None:
            return Signal.HOLD
        if not prev and short_above:
            return Signal.BUY   # 골든크로스
        if prev and not short_above:
            return Signal.SELL  # 데드크로스
        return Signal.HOLD
