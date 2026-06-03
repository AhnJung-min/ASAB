"""매매 전략 패키지."""
from .base import Signal, Strategy
from .sma_cross import SmaCrossStrategy

__all__ = ["Signal", "Strategy", "SmaCrossStrategy", "build_strategy"]


def build_strategy(cfg: dict) -> "Strategy":
    name = (cfg or {}).get("name", "sma_cross")
    if name == "sma_cross":
        return SmaCrossStrategy(
            short_window=int(cfg.get("short_window", 5)),
            long_window=int(cfg.get("long_window", 20)),
        )
    raise ValueError(f"알 수 없는 전략: {name}")
