"""KRX 호가단위(tick size) 및 지정가(limit) 계산.

순수 함수 모듈 — KIS API 의존 없음(단위테스트 용이).

시장가 주문은 호가창이 얇거나 변동성 큰 순간 원치 않게 밀려 체결되므로,
지정가 + 슬리피지 상한(band)으로 '최악 체결가'를 통제한다.

호가단위: 2023.1 개정 KOSPI 기준표를 사용한다. 각 가격대에서 KOSDAQ 틱은
이 표값의 약수이므로(예: 25만원대 KOSPI 500 / KOSDAQ 100, 500은 100의 배수),
이 표로 정렬한 가격은 KOSPI·KOSDAQ 양쪽에서 항상 유효하다 → 시장 구분 불필요.
"""
from __future__ import annotations

import math

# (상한 미만 가격, 호가단위)
_TICK_BANDS = [
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
]
_TICK_TOP = 1_000  # 50만원 이상


def tick_size(price: float) -> int:
    """가격대별 호가단위(원)."""
    for upper, tick in _TICK_BANDS:
        if price < upper:
            return tick
    return _TICK_TOP


def round_to_tick(price: float, mode: str = "down") -> int:
    """호가단위로 정렬. mode: down(내림) | up(올림) | nearest(반올림)."""
    t = tick_size(price)
    if mode == "up":
        return int(math.ceil(price / t) * t)
    if mode == "nearest":
        return int(round(price / t) * t)
    return int(price // t * t)


def limit_price(side: str, ref_price: float, band_bps: float) -> int:
    """지정가 한도 계산.

      매수: ref × (1 + band) 를 호가단위로 내림 → '이 가격 이하로만 매수'(상한 보장)
      매도: ref × (1 − band) 를 호가단위로 내림 → 살짝 낮은 한도로 체결 우선(하한 보호)

    band_bps=0 이면 현재가 기준가에 바로 건다.
    """
    band = band_bps / 10_000.0
    if side == "buy":
        return round_to_tick(ref_price * (1 + band), "down")
    if side == "sell":
        return round_to_tick(ref_price * (1 - band), "down")
    raise ValueError(f"알 수 없는 side: {side}")
