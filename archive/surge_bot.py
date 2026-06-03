"""미국 급등주 포착 + 수익실현 자동매매 (모의투자).

흐름(매 poll 주기):
  1) 잔고 조회로 실제 보유와 동기화(체결 확인)
  2) 보유 종목: 익절 / 손절 / 트레일링 스톱 조건 점검 -> 매도
  3) 빈 자리만큼: 등락률 상위에서 급등주 스캔 -> 필터 -> 매수
  4) 스캔 스냅샷과 매매 기록을 SQLite(data/market.db)에 적재 (학습용)

주문은 체결을 높이려 현재가 근처 지정가로 낸다. 체결은 다음 주기에
잔고로 확인(reconcile)하므로 비동기 체결에도 안전하다.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore
from .kis.client import KISApiError, KISClient
from .kis.config import load_config
from .kis.overseas import OverseasStock
from .portfolio import equity_snapshot


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def us_market_open(now: datetime | None = None) -> bool:
    """미국 정규장(서머타임 22:30~익05:00 KST) 대략 판정."""
    now = now or datetime.now()
    h = now.hour + now.minute / 60
    return h >= 22.5 or h < 5


class SurgeBot:
    def __init__(self):
        cfg = load_config()
        self.cfg = cfg
        self.client = KISClient(cfg)
        self.ovs = OverseasStock(self.client)
        self.store = DataStore()

        # surge 섹션은 config.yaml 최상위에 있으므로 직접 로드
        import yaml
        from pathlib import Path
        raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        s = raw.get("surge", {}) or {}

        self.exchanges = s.get("exchanges", ["NAS", "NYS"])
        self.scan_top = int(s.get("scan_top", 40))
        self.min_rate = float(s.get("min_rate", 5.0))
        self.min_volume = int(s.get("min_volume", 100000))
        self.price_min = float(s.get("price_min", 2.0))
        self.price_max = float(s.get("price_max", 500.0))
        self.max_positions = int(s.get("max_positions", 3))
        self.order_qty = int(s.get("order_qty", 1))
        self.take_profit = float(s.get("take_profit_pct", 3.0))
        self.stop_loss = float(s.get("stop_loss_pct", -2.0))
        self.trailing = float(s.get("trailing_pct", -2.0))
        self.interval = int(s.get("poll_interval_sec", 30))

        # 메모리 상태
        self.positions: dict[str, dict] = {}   # symbol -> {trade_id, entry_price, peak, qty, entry_ts, exchange, name}
        self.pending_buys: dict[str, dict] = {}  # symbol -> {entry_rate, volume, name, exchange, ts}
        self.pending_sells: dict[str, dict] = {}  # symbol -> {reason, exit_price}
        self._exchange_of: dict[str, str] = {}

        self._resume_open_trades()

    def _resume_open_trades(self) -> None:
        for t in self.store.open_trades():
            self.positions[t["symbol"]] = {
                "trade_id": t["id"], "entry_price": t["entry_price"],
                "peak": t["entry_price"], "qty": t["qty"],
                "entry_ts": t["entry_ts"], "exchange": t["exchange"], "name": t["name"],
            }
            self._exchange_of[t["symbol"]] = t["exchange"]
        if self.positions:
            log(f"이전 미청산 포지션 {len(self.positions)}개 복구")

    # --- 스캔/필터 ----------------------------------------------------------
    def scan(self) -> list[dict]:
        found: list[dict] = []
        for exch in self.exchanges:
            try:
                found.extend(self.ovs.top_gainers(exch, self.scan_top))
            except KISApiError as e:
                log(f"  스캔 오류({exch}): {e}")
        return found

    def filter_candidates(self, scanned: list[dict]) -> list[dict]:
        out = []
        for r in scanned:
            if r["rate"] < self.min_rate:
                continue
            if r["volume"] < self.min_volume:
                continue
            if not (self.price_min <= r["price"] <= self.price_max):
                continue
            if r["symbol"] in self.positions or r["symbol"] in self.pending_buys:
                continue
            out.append(r)
        out.sort(key=lambda r: r["rate"], reverse=True)
        return out

    # --- 한 주기 ------------------------------------------------------------
    def tick(self) -> None:
        now = datetime.now()
        try:
            summ = self.ovs.account_summary()
        except KISApiError as e:
            log(f"잔고 조회 오류: {e}")
            return
        held = {h["symbol"]: h for h in summ["holdings"]}
        # 계좌 자산 추이 적재 (원화 기준: 시작자본+실현+평가)
        eq = equity_snapshot(self.store, summ["holdings"], summ["fx_rate"],
                             self.cfg.initial_capital_krw)
        self.store.save_account_snapshot(now.strftime("%Y-%m-%d %H:%M:%S"), eq)

        self._reconcile_buys(held, now)
        self._adopt_orphans(held, now)
        self._reconcile_sells(held, now)
        self._check_exits(held, now)
        self._enter_new(now)

    def _adopt_orphans(self, held: dict, now: datetime) -> None:
        """DB/메모리에 없는 실제 보유분을 포지션으로 인수해 관리한다.

        재시작·외부주문·중복체결로 생긴 보유분도 익절/손절 대상에 포함된다.
        """
        for sym, h in held.items():
            if sym in self.positions or sym in self.pending_buys or h["qty"] <= 0:
                continue
            tid = self.store.open_trade({
                "symbol": sym, "name": h["name"], "exchange": self._exchange_of.get(sym, "NAS"),
                "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": h["avg_price"], "qty": h["qty"],
                "entry_rate": None, "entry_volume": None,
            })
            self.positions[sym] = {
                "trade_id": tid, "entry_price": h["avg_price"], "peak": h["avg_price"],
                "qty": h["qty"], "entry_ts": now,
                "exchange": self._exchange_of.get(sym, "NAS"), "name": h["name"],
            }
            log(f"  🔗 보유분 인수: {h['name']}({sym}) {h['qty']}주 @ ${h['avg_price']:.2f}")

    def _reconcile_buys(self, held: dict, now: datetime) -> None:
        for sym, meta in list(self.pending_buys.items()):
            if sym in held and held[sym]["qty"] > 0:
                h = held[sym]
                tid = self.store.open_trade({
                    "symbol": sym, "name": meta["name"], "exchange": meta["exchange"],
                    "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": h["avg_price"], "qty": h["qty"],
                    "entry_rate": meta["entry_rate"], "entry_volume": meta["volume"],
                })
                self.positions[sym] = {
                    "trade_id": tid, "entry_price": h["avg_price"], "peak": h["avg_price"],
                    "qty": h["qty"], "entry_ts": now, "exchange": meta["exchange"],
                    "name": meta["name"],
                }
                log(f"  ✅ 진입체결: {meta['name']}({sym}) {h['qty']}주 @ ${h['avg_price']:.2f}")
                del self.pending_buys[sym]
            elif (now - meta["ts"]).total_seconds() > 120:
                log(f"  ⏳ 미체결 매수 추적 종료: {sym}")
                del self.pending_buys[sym]

    def _reconcile_sells(self, held: dict, now: datetime) -> None:
        for sym, meta in list(self.pending_sells.items()):
            if sym not in held or held[sym]["qty"] == 0:
                pos = self.positions.get(sym)
                if pos:
                    exit_px = meta["exit_price"]
                    pnl = (exit_px - pos["entry_price"]) * pos["qty"]
                    pnl_pct = (exit_px / pos["entry_price"] - 1) * 100
                    hold_sec = int((now - pos["entry_ts"]).total_seconds()) \
                        if isinstance(pos["entry_ts"], datetime) else 0
                    self.store.close_trade(
                        pos["trade_id"], now.strftime("%Y-%m-%d %H:%M:%S"),
                        exit_px, pnl, pnl_pct, meta["reason"], hold_sec,
                    )
                    log(f"  💰 청산: {pos['name']}({sym}) @ ${exit_px:.2f} "
                        f"손익 {pnl:+.2f}$ ({pnl_pct:+.1f}%) [{meta['reason']}]")
                    self.positions.pop(sym, None)
                self.pending_sells.pop(sym, None)

    def _check_exits(self, held: dict, now: datetime) -> None:
        for sym, pos in list(self.positions.items()):
            if sym in self.pending_sells:
                continue
            if sym not in held:  # 외부에서 사라짐 -> 다음 주기 정리
                continue
            # 잔고 조회에 포함된 현재가 재활용(API 호출 절약)
            price = held[sym].get("cur_price") or 0.0
            if price <= 0:
                try:
                    price = self.ovs.current_price(self._exchange_of.get(sym, "NAS"), sym)
                except KISApiError:
                    continue
            pos["peak"] = max(pos["peak"], price)
            pnl_pct = (price / pos["entry_price"] - 1) * 100
            drawdown = (price / pos["peak"] - 1) * 100

            reason = None
            if pnl_pct >= self.take_profit:
                reason = "익절"
            elif pnl_pct <= self.stop_loss:
                reason = "손절"
            elif pos["peak"] > pos["entry_price"] and drawdown <= self.trailing:
                reason = "트레일링"

            if reason:
                limit = round(price * 0.99, 2)
                try:
                    self.ovs.sell(self._exchange_of.get(sym, "NAS"), sym, pos["qty"], limit)
                    self.pending_sells[sym] = {"reason": reason, "exit_price": price}
                    log(f"  📤 매도주문: {pos['name']}({sym}) @ ${price:.2f} [{reason}]")
                except KISApiError as e:
                    log(f"  매도 오류 {sym}: {e}")

    def _enter_new(self, now: datetime) -> None:
        room = self.max_positions - len(self.positions) - len(self.pending_buys)
        if room <= 0:
            return
        scanned = self.scan()
        if scanned:
            self.store.save_surge_scan(now.strftime("%Y-%m-%d %H:%M:%S"), scanned)
        candidates = self.filter_candidates(scanned)
        for r in candidates[:room]:
            sym = r["symbol"]
            limit = round(r["price"] * 1.005, 2)
            try:
                self.ovs.buy(r["exchange"], sym, self.order_qty, limit)
                self.pending_buys[sym] = {
                    "entry_rate": r["rate"], "volume": r["volume"],
                    "name": r["name"], "exchange": r["exchange"], "ts": now,
                }
                self._exchange_of[sym] = r["exchange"]
                log(f"  📥 매수주문: {r['name']}({sym}) +{r['rate']:.1f}% "
                    f"@ ${r['price']:.2f} -> ${limit}")
            except KISApiError as e:
                log(f"  매수 오류 {sym}: {e}")

    # --- 실행 ---------------------------------------------------------------
    def run(self, once: bool = False) -> None:
        log(f"급등주 봇 시작 | 거래소={self.exchanges} 최소등락률={self.min_rate}% "
            f"익절={self.take_profit}% 손절={self.stop_loss}% 트레일링={self.trailing}% "
            f"최대보유={self.max_positions}")
        if not us_market_open():
            log("⚠️ 현재 미국 정규장 시간이 아닙니다(주문 미체결 가능). 그래도 진행합니다.")
        if once:
            self.tick()
            self._report()
            return
        try:
            while True:
                self.tick()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log("사용자 중지.")
            self._report()

    def _report(self) -> None:
        log(f"보유 {len(self.positions)} / 매수대기 {len(self.pending_buys)} "
            f"/ 매도대기 {len(self.pending_sells)}")
        for sym, p in self.positions.items():
            log(f"   - {p['name']}({sym}) {p['qty']}주 @ ${p['entry_price']:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="미국 급등주 포착 자동매매")
    ap.add_argument("--once", action="store_true", help="1주기만 실행")
    args = ap.parse_args()
    SurgeBot().run(once=args.once)


if __name__ == "__main__":
    main()
