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

from .data.store import DataStore
from .kis.client import KISApiError, KISClient
from .kis.config import load_config
from .kis.domestic import DomesticStock
from .kis.overseas import OverseasStock
from .portfolio import target_portfolio
from .strategy import Signal, build_strategy


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def run_rotation(once: bool = False, plan: bool = False, dry_run: bool = False) -> None:
    """검증된 전략(스크리너 상위 N + 시장국면) 기반 포트폴리오 로테이션.

    매 주기마다 타깃 포트폴리오를 계산해 계좌를 리밸런싱한다.
    모든 신호·주문·계좌 스냅샷을 market.db 에 저널링한다(모의 포워드 추적).

      plan=True    : 타깃만 계산·저널(스크리너=DB만). KIS API/주문 전혀 없음.
      dry_run=True : 잔고는 조회하되 실제 주문은 내지 않음(의도 주문만 기록).
    """
    cfg = load_config()
    s = cfg.strategy
    t = cfg.trading
    top_n = int(s.get("top_n", 5))
    regime_filter = bool(s.get("regime_filter", True))
    regime_ma = int(s.get("regime_ma", 200))
    interval = int(t.get("rebalance_interval_sec", t.get("poll_interval_sec", 3600)))
    invest_ratio = float(t.get("invest_ratio", 0.95))        # 가용현금 중 투자 비율
    max_value = int(t.get("max_position_value_krw", 0))      # 종목당 최대 매수금액(0=무제한)

    if dry_run:
        mode = "DRY-RUN(주문안함)"
    elif plan:
        mode = "PLAN(API없음)"
    else:
        mode = "모의투자" if cfg.paper_trading else "!! 실전투자 !!"
    log(f"로테이션 봇 시작 [{mode}] top{top_n} · 국면필터={regime_filter}(MA{regime_ma})")

    store = DataStore()
    dom: DomesticStock | None = None
    if not plan:
        dom = DomesticStock(KISClient(cfg))
    order_mode = "dryrun" if dry_run else ("paper" if cfg.paper_trading else "real")

    def journal(picks: list[dict], action: str, asof: str | None, reason: str | None = None) -> None:
        rows = [{"symbol": p["symbol"], "name": p.get("name"), "rank": p.get("rank"),
                 "score": p.get("score"), "action": action, "reason": reason} for p in picks]
        if rows:
            store.add_live_signals(_now(), "screener_rotation", asof, rows)

    def place(side: str, symbol: str, name: str, qty: int, est_price: int) -> None:
        rec = {"ts": _now(), "symbol": symbol, "name": name, "side": side,
               "qty": qty, "price": 0, "ord_dvsn": "01", "mode": order_mode}
        if dry_run:
            rec.update(status="dryrun", rt_cd="", msg="(dry-run 미주문)", order_no="")
            store.add_live_order(rec)
            log(f"    [dry] {side} {name}({symbol}) x{qty} ~{est_price:,}원")
            return
        try:
            res = dom.buy(symbol, qty) if side == "buy" else dom.sell(symbol, qty)
            rec.update(status="sent", rt_cd=str(res.get("rt_cd", "")),
                       msg=res.get("msg1", ""),
                       order_no=str((res.get("output") or {}).get("ODNO", "")))
            log(f"    {side} {name}({symbol}) x{qty}: {res.get('msg1', res)}")
        except KISApiError as e:
            rec.update(status="rejected", rt_cd="", msg=str(e), order_no="")
            log(f"    {side} {name}({symbol}) x{qty} 실패: {e}")
        store.add_live_order(rec)

    def cycle() -> None:
        tp = target_portfolio(store, top_n=top_n, regime_filter=regime_filter, regime_ma=regime_ma)
        asof = tp["asof"]
        state = "risk-ON(투자)" if tp["regime_on"] else "risk-OFF(현금)"
        log(f"기준일 {asof} · 시장국면 {state} · 타깃 {len(tp['picks'])}종목")
        journal(tp["picks"], "target", asof)
        for p in tp["picks"]:
            log(f"  타깃 {p['rank']}. {p['name']}({p['symbol']}) score={p['score']}")

        if plan:
            return  # API 미사용: 타깃만 기록하고 종료

        bal = dom.balance()
        held = {h["symbol"]: h for h in bal["holdings"]}
        cash = bal["cash"]
        target_syms = {p["symbol"] for p in tp["picks"]}

        # 1) 매도: 타깃에 없는 보유 종목 전량 청산 (국면 off 면 모두 청산됨)
        for sym, h in held.items():
            if sym in target_syms:
                continue
            journal([{"symbol": sym, "name": h["name"]}], "sell", asof, "타깃 이탈/청산")
            place("sell", sym, h["name"], h["qty"], int(h["avg_price"]))

        # 2) 매수: 보유에 없는 타깃 종목, 가용현금 동일비중
        buys = [p for p in tp["picks"] if p["symbol"] not in held]
        per = (cash * invest_ratio / len(buys)) if buys else 0.0
        for p in buys:
            sym = p["symbol"]
            try:
                price = dom.current_price(sym)
            except KISApiError as e:
                journal([p], "skip", asof, f"시세조회 실패: {e}")
                log(f"  매수 보류 {p['name']}({sym}): 시세 오류 {e}")
                continue
            alloc = per if max_value <= 0 else min(per, max_value)
            qty = int(alloc // price) if price > 0 else 0
            if qty < 1:
                journal([p], "skip", asof, f"예산부족(배정 {per:,.0f}원 < 1주 {price:,}원)")
                log(f"  매수 보류 {p['name']}({sym}): 예산부족")
                continue
            journal([p], "buy", asof, f"신규 진입(배정 ~{per:,.0f}원)")
            place("buy", sym, p["name"], qty, price)

        # 3) 리밸런싱 후 계좌 스냅샷 저널
        try:
            bal2 = dom.balance()
            hk = sum(h["eval_amt"] for h in bal2["holdings"])
            uk = sum(h["pnl"] for h in bal2["holdings"])
            store.save_account_snapshot(_now(), {
                "cash_krw": bal2["cash"], "holdings_krw": hk,
                "total_krw": bal2["cash"] + hk, "realized_krw": 0.0,
                "unrealized_krw": uk, "fx_rate": 1.0})
        except KISApiError:
            pass

    try:
        if once or plan:
            cycle()
        else:
            log(f"리밸런스 주기 {interval}초. 중지하려면 Ctrl+C.")
            while True:
                cycle()
                time.sleep(interval)
    except KeyboardInterrupt:
        log("사용자 중지. 봇 종료.")
    finally:
        store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="KIS 모의투자 자동매매 봇")
    ap.add_argument("--once", action="store_true", help="1회만 실행하고 종료")
    ap.add_argument("--plan", action="store_true",
                    help="[로테이션] 타깃만 계산·저널 (KIS API/주문 없음)")
    ap.add_argument("--dry-run", action="store_true",
                    help="[로테이션] 잔고는 조회하되 실제 주문은 내지 않음")
    args = ap.parse_args()

    cfg = load_config()
    name = (cfg.strategy or {}).get("name", "sma_cross")
    if name == "screener_rotation":
        run_rotation(once=args.once, plan=args.plan, dry_run=args.dry_run)
    else:
        if args.plan or args.dry_run:
            log("주의: --plan/--dry-run 은 screener_rotation 전략에서만 동작합니다. "
                "config.yaml 의 strategy.name 을 확인하세요.")
        run(once=args.once)


if __name__ == "__main__":
    main()
