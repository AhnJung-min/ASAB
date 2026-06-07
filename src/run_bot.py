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
from .portfolio import target_portfolio
from .pricing import limit_price
from .strategy import Signal, build_strategy


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def exit_reason(entry: float, peak: float, cur: float,
                stop_loss: float, trailing: float, take_profit: float) -> str | None:
    """리스크 청산 사유 판정(순수함수). 모두 % 단위(양수), 0=비활성.

      익절   : 손익률 >= take_profit
      손절   : 손익률 <= -stop_loss
      트레일링: 고점보다 올랐던 종목이 고점 대비 -trailing 이상 하락
    우선순위 익절 > 손절 > 트레일링. 해당 없으면 None.
    """
    if entry <= 0:
        return None
    pnl_pct = (cur / entry - 1) * 100
    dd_pct = (cur / peak - 1) * 100 if peak > 0 else 0.0
    if take_profit > 0 and pnl_pct >= take_profit:
        return "익절"
    if stop_loss > 0 and pnl_pct <= -stop_loss:
        return "손절"
    if trailing > 0 and peak > entry and dd_pct <= -trailing:
        return "트레일링"
    return None


def run(once: bool = False) -> None:
    cfg = load_config()
    mode = "모의투자" if cfg.paper_trading else "!! 실전투자 !!"
    log(f"봇 시작 ({mode}) 계좌 {cfg.account_no}-{cfg.account_product_code}")

    client = KISClient(cfg)
    dom = DomesticStock(client)
    strategy = build_strategy(cfg.strategy)

    t = cfg.trading
    dom_symbols: list[str] = t.get("domestic_symbols", [])
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

    2중 매도 구조:
      [1층] 전략적 청산 — 타깃(상위 N) 이탈 / 시장국면 off  (리밸런스 시, 보통 1일 1회)
      [2층] 리스크 청산 — 손절 / 트레일링스톱 / 익절        (감시 주기, 보통 분 단위)
    매수는 지정가(시장가 밀림 방지)로, 슬리피지 상한 안에서만 체결한다.
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
    risk_interval = int(t.get("risk_check_interval_sec", 60))   # 리스크(손절) 감시 주기
    quote_market = str(t.get("quote_market", "UN")).upper()     # 시세 시장: UN(통합)/J(KRX)/NX
    invest_ratio = float(t.get("invest_ratio", 0.95))           # 가용현금 중 투자 비율
    max_value = int(t.get("max_position_value_krw", 0))         # 종목당 최대 매수금액(0=무제한)
    order_type = str(t.get("order_type", "limit")).lower()      # limit(지정가) | market(시장가)
    limit_band = float(t.get("limit_band_bps", 30))             # 매수 지정가 상한(현재가 대비 bps)
    exit_band = float(t.get("exit_band_bps", 50))               # 매도 지정가(체결 우선 위해 넓게)
    stop_loss = float(s.get("stop_loss_pct", 0))               # 손절 %(0=비활성)
    trailing = float(s.get("trailing_pct", 0))                 # 트레일링스톱 %(0=비활성)
    take_profit = float(s.get("take_profit_pct", 0))           # 익절 %(0=비활성)
    risk_on = stop_loss > 0 or trailing > 0 or take_profit > 0

    if dry_run:
        mode = "DRY-RUN(주문안함)"
    elif plan:
        mode = "PLAN(API없음)"
    else:
        mode = "모의투자" if cfg.paper_trading else "!! 실전투자 !!"
    risk_txt = (f"손절{stop_loss}% 트레일링{trailing}% 익절{take_profit}%"
                if risk_on else "리스크청산 OFF")
    log(f"로테이션 봇 시작 [{mode}] top{top_n} · 국면필터={regime_filter}(MA{regime_ma})")
    mkt_txt = {"UN": "통합(KRX+NXT)", "NX": "NXT", "J": "KRX"}.get(quote_market, quote_market)
    log(f"  주문={order_type}(매수상한{limit_band}bps) · 시세={mkt_txt} · {risk_txt}")

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

    def place(side: str, symbol: str, name: str, qty: int, ref_price: float,
              band_bps: float, reason: str = "") -> None:
        # 지정가: 현재가 기준 한도 계산(매수는 상한, 매도는 체결우선 하한). 0이면 시장가.
        if order_type == "limit" and ref_price > 0:
            px = limit_price(side, ref_price, band_bps)
            ord_dvsn = "00"
        else:
            px = 0
            ord_dvsn = "01"
        px_txt = f"지정가{px:,}" if px else "시장가"
        rec = {"ts": _now(), "symbol": symbol, "name": name, "side": side,
               "qty": qty, "price": px, "ord_dvsn": ord_dvsn, "mode": order_mode}
        tag = f"[{reason}] " if reason else ""
        if dry_run:
            rec.update(status="dryrun", rt_cd="", msg="(dry-run 미주문)", order_no="")
            store.add_live_order(rec)
            log(f"    [dry] {tag}{side} {name}({symbol}) x{qty} {px_txt}")
            return
        try:
            res = (dom.buy(symbol, qty, px) if side == "buy"
                   else dom.sell(symbol, qty, px))
            rec.update(status="sent", rt_cd=str(res.get("rt_cd", "")),
                       msg=res.get("msg1", ""),
                       order_no=str((res.get("output") or {}).get("ODNO", "")))
            log(f"    {tag}{side} {name}({symbol}) x{qty} {px_txt}: {res.get('msg1', res)}")
        except KISApiError as e:
            rec.update(status="rejected", rt_cd="", msg=str(e), order_no="")
            log(f"    {tag}{side} {name}({symbol}) x{qty} {px_txt} 실패: {e}")
        store.add_live_order(rec)

    def account_snapshot() -> None:
        try:
            bal = dom.balance()
            hk = sum(h["eval_amt"] for h in bal["holdings"])
            uk = sum(h["pnl"] for h in bal["holdings"])
            store.save_account_snapshot(_now(), {
                "cash_krw": bal["cash"], "holdings_krw": hk,
                "total_krw": bal["cash"] + hk, "realized_krw": 0.0,
                "unrealized_krw": uk, "fx_rate": 1.0})
        except KISApiError:
            pass

    def risk_pass() -> None:
        """[2층] 보유 종목 손절/트레일링/익절 점검. 고점(peak)을 영속화한다."""
        if not risk_on:
            return
        try:
            bal = dom.balance()
        except KISApiError as e:
            log(f"  리스크 점검 보류(잔고 오류): {e}")
            return
        held = {h["symbol"]: h for h in bal["holdings"]}
        positions = store.get_positions()
        # 더 이상 보유하지 않는 포지션 정리(외부 청산/체결)
        for sym in list(positions):
            if sym not in held and not dry_run:
                store.delete_position(sym)
        for sym, h in held.items():
            try:
                cur = dom.current_price(sym, quote_market)   # 통합시세=NXT 연장시간 반영
            except KISApiError:
                continue
            entry = float(h["avg_price"]) or cur
            pos = positions.get(sym)
            peak = max(pos["peak_price"], cur) if pos else max(entry, cur)
            opened = pos["opened_ts"] if pos else _now()
            store.save_position(sym, h["name"], entry, peak, h["qty"], opened, _now())
            pnl_pct = (cur / entry - 1) * 100 if entry > 0 else 0.0
            dd_pct = (cur / peak - 1) * 100 if peak > 0 else 0.0
            reason = exit_reason(entry, peak, cur, stop_loss, trailing, take_profit)
            if reason:
                log(f"  🛑 {reason}: {h['name']}({sym}) 손익{pnl_pct:+.1f}% "
                    f"고점대비{dd_pct:+.1f}% (현재 {cur:,})")
                journal([{"symbol": sym, "name": h["name"]}], "sell", None,
                        f"{reason}(손익{pnl_pct:+.1f}%)")
                place("sell", sym, h["name"], h["qty"], cur, exit_band, reason)
                if not dry_run:
                    store.delete_position(sym)

    def rebalance() -> None:
        """[1층] 타깃 재계산 후 이탈 청산 + 신규 동일비중 지정가 매수."""
        tp = target_portfolio(store, top_n=top_n, regime_filter=regime_filter, regime_ma=regime_ma)
        asof = tp["asof"]
        state = "risk-ON(투자)" if tp["regime_on"] else "risk-OFF(현금)"
        log(f"기준일 {asof} · 시장국면 {state} · 타깃 {len(tp['picks'])}종목")
        journal(tp["picks"], "target", asof)
        for p in tp["picks"]:
            log(f"  타깃 {p['rank']}. {p['name']}({p['symbol']}) score={p['score']}")

        if plan:
            return  # API 미사용: 타깃만 기록

        bal = dom.balance()
        held = {h["symbol"]: h for h in bal["holdings"]}
        cash = bal["cash"]
        target_syms = {p["symbol"] for p in tp["picks"]}

        # 1) 매도: 타깃에 없는 보유 종목 전량 청산(국면 off 면 모두 청산)
        for sym, h in held.items():
            if sym in target_syms:
                continue
            try:
                ref = dom.current_price(sym, quote_market)
            except KISApiError:
                ref = float(h["avg_price"])
            journal([{"symbol": sym, "name": h["name"]}], "sell", asof, "타깃 이탈/청산")
            place("sell", sym, h["name"], h["qty"], ref, exit_band, "타깃이탈")
            if not dry_run:
                store.delete_position(sym)

        # 2) 매수: 보유에 없는 타깃 종목, 가용현금 동일비중 지정가
        buys = [p for p in tp["picks"] if p["symbol"] not in held]
        per = (cash * invest_ratio / len(buys)) if buys else 0.0
        for p in buys:
            sym = p["symbol"]
            try:
                price = dom.current_price(sym, quote_market)
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
            place("buy", sym, p["name"], qty, price, limit_band, "신규진입")

        account_snapshot()

    try:
        if plan:
            rebalance()  # 타깃만 계산·저널
        elif once:
            risk_pass()       # 먼저 기존 보유 보호
            rebalance()       # 그다음 로테이션
            account_snapshot()
        else:
            log(f"감시 주기 {risk_interval}초(리스크) · 리밸런스 1일 1회. 중지 Ctrl+C.")
            last_day: str | None = None
            while True:
                today = datetime.now().strftime("%Y%m%d")
                if last_day != today:
                    rebalance()
                    last_day = today
                risk_pass()
                account_snapshot()
                time.sleep(risk_interval)
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
