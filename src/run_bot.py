"""자동매매 봇 진입점.

실행:  python -m src.run_bot           (config.yaml 사용)
       python -m src.run_bot --once   (1회만 점검하고 종료)

설정된 감시 종목의 현재가를 주기적으로 조회해 전략 신호에 따라
모의투자 계좌에서 매수/매도한다.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 깨짐 방지

from .kis.client import KISApiError, KISClient
from .kis.config import load_config
from .kis.domestic import DomesticStock
from .kis.overseas import OverseasStock
from .strategy import Signal, build_strategy


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(once: bool = False) -> None:
    cfg = load_config()
    mode = "모의투자" if cfg.paper_trading else "!! 실전투자 !!"
    log(f"봇 시작 ({mode}) 계좌 {cfg.account_no}-{cfg.account_product_code}")

    client = KISClient(cfg)
    dom = DomesticStock(client)
    ovs = OverseasStock(client)
    strategy = build_strategy(cfg.strategy)

    t = cfg.trading
    dom_symbols: list[str] = t.get("domestic_symbols", [])
    ovs_symbols: list[dict] = t.get("overseas_symbols", [])
    interval = int(t.get("poll_interval_sec", 10))
    qty = int(t.get("order_qty", 1))
    max_pos = int(t.get("max_position_per_symbol", 5))

    def handle(key: str, price: float, signal: Signal, do_buy, do_sell, get_pos) -> None:
        log(f"  {key}: 가격={price} 신호={signal.value}")
        if signal is Signal.BUY:
            if get_pos() >= max_pos:
                log(f"    -> 매수 보류 (최대 보유 {max_pos} 도달)")
                return
            res = do_buy()
            log(f"    -> 매수 주문: {res.get('msg1', res)}")
        elif signal is Signal.SELL:
            if get_pos() <= 0:
                log("    -> 매도 보류 (보유 없음)")
                return
            res = do_sell()
            log(f"    -> 매도 주문: {res.get('msg1', res)}")

    def tick() -> None:
        # 국내
        for sym in dom_symbols:
            try:
                price = dom.current_price(sym)
                sig = strategy.update(f"KR:{sym}", price)
                handle(
                    f"KR {sym}", price, sig,
                    do_buy=lambda s=sym: dom.buy(s, qty),
                    do_sell=lambda s=sym: dom.sell(s, qty),
                    get_pos=lambda s=sym: dom.position_qty(s),
                )
            except KISApiError as e:
                log(f"  KR {sym}: API 오류 {e}")

        # 해외
        for item in ovs_symbols:
            exch, sym = item["exchange"], item["symbol"]
            try:
                price = ovs.current_price(exch, sym)
                sig = strategy.update(f"US:{sym}", price)
                handle(
                    f"US {sym}", price, sig,
                    do_buy=lambda e=exch, s=sym, p=price: ovs.buy(e, s, qty, p),
                    do_sell=lambda e=exch, s=sym, p=price: ovs.sell(e, s, qty, p),
                    get_pos=lambda s=sym: ovs.position_qty(s),
                )
            except KISApiError as e:
                log(f"  US {sym}: API 오류 {e}")

    if once:
        tick()
        return

    log(f"감시 주기 {interval}초. 중지하려면 Ctrl+C.")
    try:
        while True:
            tick()
            time.sleep(interval)
    except KeyboardInterrupt:
        log("사용자 중지. 봇 종료.")


def main() -> None:
    ap = argparse.ArgumentParser(description="KIS 모의투자 자동매매 봇")
    ap.add_argument("--once", action="store_true", help="1회만 실행하고 종료")
    args = ap.parse_args()
    run(once=args.once)


if __name__ == "__main__":
    main()
