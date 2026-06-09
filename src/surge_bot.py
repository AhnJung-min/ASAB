"""국내 급등주 포착 + 수익실현 자동매매 (모의투자).

일봉 추세 트랙(run_bot screener_rotation)과 별개인 **장중 단타 트랙**이다.
선정=일봉으로 충분하지만 단타는 분/초 단위 실시간 신호가 필요하므로 분리한다.

흐름(매 poll 주기):
  1) 잔고 조회로 실제 보유와 동기화(체결 확인)
  2) 보유 종목: 익절 / 손절 / 트레일링 스톱 점검 -> 매도
  3) 빈 자리만큼: 등락률 상위 스캔 -> 필터 -> 매수
  4) 스캔 스냅샷과 매매 기록을 SQLite(data/market.db)에 적재 (학습용)

주문은 체결을 높이려 현재가 근처 지정가(KRX 호가단위)로 낸다. 체결은 다음
주기에 잔고로 확인(reconcile)하므로 비동기 체결에도 안전하다.

검증 현실: 분봉 과거데이터가 없어 일봉 백테스트처럼 못 한다. 모의계좌
포워드로 surge_trade(학습데이터)를 쌓고, 모이면 ML로 진입 필터를 학습한다.

실행:  python -m src.surge_bot              (주기 반복)
       python -m src.surge_bot --once       (1주기만)
       python -m src.surge_bot --dry-run    (주문 없이 스캔·신호만 — 안전 점검)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 깨짐 방지

import yaml

from .data.store import DataStore
from .kis.client import KISApiError, KISClient
from .kis.config import load_config
from .kis.domestic import DomesticStock
from .pricing import limit_price


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# 레버리지·인버스 파생상품 종목명 키워드(대문자 비교). 단타 추세가정과 안 맞고
# decay/청산위험이 있어 기본 제외. 일반 ETF/ETN 은 남긴다.
_LEV_INV_KEYWORDS = ("레버리지", "인버스", "2X", "3X", "곱버스")


def is_leverage_inverse(name: str) -> bool:
    up = (name or "").upper()
    return any(k in up for k in _LEV_INV_KEYWORDS)


# ETF/ETN 브랜드 접두어. 거래대금 상위엔 지수 ETF가 섞이므로 '개별주만' 위해 제외.
_ETF_BRANDS = ("KODEX", "TIGER", "KBSTAR", "ARIRANG", "KOSEF", "HANARO", "ACE",
               "SOL", "RISE", "PLUS", "KINDEX", "TIMEFOLIO", "WOORI", "BNK",
               "FOCUS", "히어로즈", "마이티", "korea")


def is_etf(name: str) -> bool:
    up = (name or "").upper()
    return any(up.startswith(b.upper()) or (" " + b.upper()) in up for b in _ETF_BRANDS)


def kr_market_open(now: datetime | None = None) -> bool:
    """국내 정규장(평일 09:00~15:30 KST) 대략 판정."""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 토/일
        return False
    h = now.hour + now.minute / 60
    return 9.0 <= h <= 15.5


class SurgeBot:
    def __init__(self, dry_run: bool = False):
        cfg = load_config()
        self.cfg = cfg
        self.dry_run = dry_run
        self.client = KISClient(cfg)
        self.dom = DomesticStock(self.client)
        self.store = DataStore()

        # surge 섹션은 config.yaml 최상위에 있으므로 직접 로드
        raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        s = raw.get("surge", {}) or {}

        self.market = str(s.get("market", "0000"))      # 0000전체/0001코스피/1001코스닥
        self.scan_source = str(s.get("scan_source", "both")).lower()  # rate|value|both
        self.scan_top = int(s.get("scan_top", 40))
        self.value_top = int(s.get("value_top", 40))     # 거래대금 상위 스캔 수
        self.exclude_etf = bool(s.get("exclude_etf", True))  # 지수 ETF 제외(개별주만)
        self.min_rate = float(s.get("min_rate", 5.0))    # 최소 등락률%
        self.max_rate = float(s.get("max_rate", 20.0))   # 상한가 추격 방지(체결 불가)
        self.min_volume = int(s.get("min_volume", 100_000))
        self.price_min = int(s.get("price_min", 1_000))  # 동전주 배제
        self.price_max = int(s.get("price_max", 100_000))
        self.exclude_lev = bool(s.get("exclude_leverage_inverse", True))  # 레버리지·인버스 제외
        self.max_positions = int(s.get("max_positions", 3))
        self.order_value_krw = int(s.get("order_value_krw", 0))  # 종목당 배정금(0=order_qty)
        self.order_qty = int(s.get("order_qty", 1))
        self.take_profit = float(s.get("take_profit_pct", 3.0))
        self.stop_loss = float(s.get("stop_loss_pct", -2.0))    # 음수
        self.trailing = float(s.get("trailing_pct", -2.0))      # 음수
        # 보유 시간 초과 청산(분). 손익절에 안 걸리고 정체된 종목을 강제 청산→리밸런싱.
        # 0이면 비활성. 데이터로도 '정체 진입'의 결과(reason=시간초과)가 라벨로 남음.
        self.max_hold_sec = int(float(s.get("max_hold_min", 30)) * 60)
        self.buy_band_bps = float(s.get("buy_band_bps", 30))    # 매수 지정가 상한
        self.sell_band_bps = float(s.get("sell_band_bps", 50))  # 매도 체결우선 하한
        self.entry_order = str(s.get("entry_order_type", "limit")).lower()  # limit|market|best
        self.quote_market = str(s.get("quote_market", "UN")).upper()  # UN통합/J KRX/NX
        self.interval = int(s.get("poll_interval_sec", 30))
        self.ob_capture_top = int(s.get("ob_capture_top", 8))  # 호가 임밸런스 수집할 상위 후보수
        # === 종목 선정 기준 (유동성·매수세 우위 중심) ===
        # 매수 전 호가 임밸런스가 이 값 이상인 종목만 매수(0=매수세 우위, 음수허용=-1)
        self.min_imbalance = float(s.get("min_imbalance", 0.0))
        # 최종 매수 정렬 기준: imbalance(매수세) | value(거래대금) | rate(등락률)
        self.rank_by = str(s.get("rank_by", "imbalance")).lower()
        # === 하락 반등 데이터 수집 (수집 전용 — 매수는 안 함) ===
        # 하락률 상위 종목도 스캔해 surge_scan 에 적재(source='rebound'). 음수 등락률이라
        # 기존 매수 필터(min_rate>0)에 자동으로 안 걸려 매수 대상은 아니다.
        self.scan_rebound = bool(s.get("scan_rebound", True))
        self.rebound_top = int(s.get("rebound_top", 40))      # 하락률 상위 몇 종목 스캔
        # 그 중 호가 임밸런스 수집할 상위 N(반등신호=매수세 복귀; API비용 고려 소수)
        self.rebound_ob_top = int(s.get("rebound_ob_top", 4))
        # 재진입 쿨다운(분): 판 종목을 이 시간 동안 다시 안 산다(같은 종목 churn·휩쏘 방지).
        self.reentry_cooldown_sec = int(float(s.get("reentry_cooldown_min", 10)) * 60)
        self.regime_filter = bool(s.get("regime_filter", True))   # 지수 급락 시 매수 중단
        self.regime_index = str(s.get("regime_index", "069500"))  # 국면 프록시(KODEX200)
        self.regime_min_chg = float(s.get("regime_min_chg", -1.5))  # 지수 등락률 이 미만이면 OFF
        self.use_model = bool(s.get("use_model", True))       # 학습모델로 후보 점수화
        self.min_pred_ret = float(s.get("min_pred_ret", -1.0))  # 예측수익 이 미만 후보 제외(-1=제외안함)
        self._model_bundle: dict | None = None
        self._model_mtime = 0.0

        # 메모리 상태
        self.positions: dict[str, dict] = {}    # symbol -> {trade_id, entry_price, peak, qty, entry_ts, name}
        self.pending_buys: dict[str, dict] = {}  # symbol -> {entry_rate, volume, name, ts}
        self.pending_sells: dict[str, dict] = {}  # symbol -> {reason, exit_price}
        self.recent_exits: dict[str, datetime] = {}  # symbol -> 마지막 청산시각(재진입 쿨다운)

        if not dry_run:
            self._resume_open_trades()

    def _resume_open_trades(self) -> None:
        for t in self.store.open_trades():
            if t["entry_rate"] is None:
                continue  # entry_rate 없는 행=옛 인수(orphan) 잔재 → 관리 대상 아님
            self.positions[t["symbol"]] = {
                "trade_id": t["id"], "entry_price": t["entry_price"],
                "peak": t["entry_price"], "qty": t["qty"],
                "entry_ts": t["entry_ts"], "name": t["name"],
            }
        if self.positions:
            log(f"이전 미청산 포지션 {len(self.positions)}개 복구")

    # --- 스캔/필터 ----------------------------------------------------------
    def scan(self) -> list[dict]:
        """등락률 상위 + 거래대금 상위(유동성 풍부)를 합쳐 후보 풀 구성(중복 제거).

        source 태그를 달아 둔다(rate=급등 추격, value=유동성 단타). 매수 조건은
        동일 필터(오르는 중+유동성+비레버리지/ETF)라 거래대금 유니버스에선
        '유동성 좋고 오늘 상승 중'인 우량주만 자연히 걸린다(새 임계값 없음).
        """
        out: list[dict] = []
        seen: set[str] = set()
        jobs = []
        # 거래대금(유동성) 소스를 먼저 — 중복 종목은 거래대금(value) 있는 쪽을 유지
        if self.scan_source in ("value", "both"):
            jobs.append(("value", lambda: self.dom.top_value(
                self.value_top, market=self.market,
                min_price=self.price_min, min_volume=self.min_volume)))
        if self.scan_source in ("rate", "both"):
            jobs.append(("rate", lambda: self.dom.top_gainers(
                self.scan_top, gubn="up", market=self.market,
                min_price=self.price_min, min_volume=self.min_volume)))
        # 하락률 상위(반등 후보) — 데이터 수집 전용. 매수 안 함(음수 등락률).
        if self.scan_rebound:
            jobs.append(("rebound", lambda: self.dom.top_gainers(
                self.rebound_top, gubn="down", market=self.market,
                min_price=self.price_min, min_volume=self.min_volume)))
        for src, fn in jobs:
            try:
                for r in fn():
                    if r["symbol"] in seen:
                        continue
                    seen.add(r["symbol"])
                    r["source"] = src
                    out.append(r)
            except KISApiError as e:
                log(f"  스캔 오류({src}): {e}")
        return out

    def _current_model(self) -> dict | None:
        """학습 모델을 로드하되, 파일이 갱신되면(재학습) 자동 재로드(연속학습)."""
        from .surge_ml import MODEL_PATH, load_model
        try:
            m = MODEL_PATH.stat().st_mtime
        except OSError:
            return None
        if m != self._model_mtime:
            self._model_bundle = load_model()
            self._model_mtime = m
            if self._model_bundle:
                meta = self._model_bundle.get("meta", {})
                log(f"  🤖 ML 모델 로드(학습 {meta.get('rows','?')}행, "
                    f"OOS IC {meta.get('oos_ic')})")
        return self._model_bundle

    def _index_change(self) -> float:
        """시장 국면 프록시 지수 등락률%. 조회 실패 시 0(중립=매수 허용)."""
        try:
            return self.dom.index_change(self.regime_index)
        except KISApiError:
            return 0.0

    def _capture_orderbook(self, candidates: list[dict], now: datetime) -> None:
        """상위 후보의 호가 잔량 임밸런스 수집(호출제한 위해 상위 N개만).
        후보에 ob_imbalance 부착 + surge_orderbook 적재(학습 피처). 단타 핵심."""
        top = candidates[: self.ob_capture_top]
        obs = []
        for c in top:
            try:
                ob = self.dom.order_book(c["symbol"])
            except KISApiError:
                continue
            c["ob_imbalance"] = ob["imbalance"]
            obs.append({"symbol": c["symbol"], **ob})
        if obs:
            self.store.save_surge_orderbook(now.strftime("%Y-%m-%d %H:%M:%S"), obs)

    def _capture_rebound_orderbook(self, scanned: list[dict], now: datetime) -> None:
        """하락 반등 후보(source='rebound') 중 유동성 상위 N개의 호가 임밸런스 수집.

        반등의 핵심 신호 = 떨어지던 종목에 매수세(양의 임밸런스)가 돌아오는 것.
        매수는 안 하지만, 이 신호를 surge_orderbook 에 남겨 ML이 학습하게 한다.
        (API 비용 위해 rebound_ob_top 소수만.)
        """
        if self.rebound_ob_top <= 0:
            return
        reb = [r for r in scanned if r.get("source") == "rebound"]
        reb.sort(key=lambda r: r.get("value", 0), reverse=True)  # 유동성 우선
        self._capture_orderbook(reb[: self.rebound_ob_top], now)

    def _apply_model(self, candidates: list[dict], n_scan: int) -> None:
        """모델이 있으면 후보에 예측수익 점수를 매겨 재정렬하고 하한 미만은 제외."""
        if not self.use_model or not candidates:
            return
        bundle = self._current_model()
        if not bundle:
            return  # 모델 없으면 기존(등락률순) 유지
        from .surge_ml import score_candidates
        score_candidates(bundle, candidates, n_scan)
        candidates[:] = [c for c in candidates if c.get("ml_score", 0.0) >= self.min_pred_ret]
        candidates.sort(key=lambda c: -c.get("ml_score", 0.0))

    def _rank_by_pressure(self, candidates: list[dict]) -> None:
        """매수세 우위 필터 + 정렬(in-place). 모델이 없을 때의 종목 선정 규칙.

        호가 임밸런스는 상위 후보(ob_capture_top)만 수집되므로, 임밸런스가
        없는(미수집) 후보는 매수 대상에서 제외한다(보수적 — 호가 확인된 것만).
        그 중 매수세 우위(imbalance >= min_imbalance)만 남기고 rank_by 로 정렬.
        """
        if not candidates:
            return
        # 매수 제외 = '호가가 확인됐고 매도세 우위(imbalance < min)'인 경우만.
        # 호가 미수집(None=한도 등으로 못 받음)은 제외하지 않는다 — 호가 실패가
        # 거래를 막아 봇이 멈추는 취약점 방지(거래대금 기준으로라도 매수).
        def _keep(c: dict) -> bool:
            ob = c.get("ob_imbalance")
            return ob is None or ob >= self.min_imbalance
        scored = [c for c in candidates if _keep(c)]
        # 정렬: 매수세 우위(임밸런스 큰)부터, 호가 미수집은 그다음(거래대금순).
        # rank_by 와 무관하게 '있는 임밸런스 우선 → 없으면 거래대금' 으로 안정화.
        def _key(c: dict):
            ob = c.get("ob_imbalance")
            has = ob is not None
            primary = ob if has else 0.0
            if self.rank_by == "value":
                primary = c.get("value", 0)
            elif self.rank_by == "rate":
                primary = c.get("rate", 0.0)
            return (has, primary, c.get("value", 0))
        scored.sort(key=_key, reverse=True)
        candidates[:] = scored

    def filter_candidates(self, scanned: list[dict]) -> list[dict]:
        out = []
        for r in scanned:
            if self.exclude_lev and is_leverage_inverse(r["name"]):
                continue  # 레버리지·인버스 파생상품 제외
            if self.exclude_etf and is_etf(r["name"]):
                continue  # 지수 ETF/ETN 제외(개별주만 단타)
            if not (self.min_rate <= r["rate"] <= self.max_rate):
                continue
            if r["volume"] < self.min_volume:
                continue
            if not (self.price_min <= r["price"] <= self.price_max):
                continue
            if r["symbol"] in self.positions or r["symbol"] in self.pending_buys:
                continue
            # 재진입 쿨다운: 최근 판 종목은 일정시간 다시 안 산다(같은 종목 churn 방지)
            ex = self.recent_exits.get(r["symbol"])
            if ex and (datetime.now() - ex).total_seconds() < self.reentry_cooldown_sec:
                continue
            out.append(r)
        # 거래대금(유동성) 높은 순 — 호가 임밸런스를 '유동성 좋은 후보부터' 수집하기 위함
        out.sort(key=lambda r: r.get("value", 0), reverse=True)
        return out

    # --- 한 주기 ------------------------------------------------------------
    def tick(self) -> None:
        now = datetime.now()
        try:
            bal = self.dom.balance()
        except KISApiError as e:
            # 모의 서버 원장 초당 한도(EGW00201)는 비치명적 — 이번 주기만 건너뛰고 복구.
            # 로그를 더럽히지 않게 조용히 넘어간다(다른 오류만 표시).
            if e.code != "EGW00201":
                log(f"잔고 조회 오류: {e}")
            return
        held = {h["symbol"]: h for h in bal["holdings"]}
        if self.dry_run:
            # 주문·DB 변경 없이 스캔→필터→매수신호만 점검(보유 인수도 안 함)
            self._enter_new(now)
            return
        self._save_account(now, bal)

        self._reconcile_buys(held, now)
        self._reconcile_sells(held, now)
        self._cleanup_vanished(held, now)
        self._check_exits(held, now)
        self._enter_new(now)

    def _save_account(self, now: datetime, bal: dict) -> None:
        hk = sum(h["eval_amt"] for h in bal["holdings"])
        uk = sum(h["pnl"] for h in bal["holdings"])
        self.store.save_account_snapshot(now.strftime("%Y-%m-%d %H:%M:%S"), {
            "cash_krw": bal["cash"], "holdings_krw": hk,
            "total_krw": bal.get("total") or (bal["cash"] + hk),
            "realized_krw": 0.0, "unrealized_krw": uk, "fx_rate": 1.0})

    # 주의: 예전엔 _adopt_orphans 로 계좌의 모든 보유분을 인수해 관리했으나(미국
    # 단독계좌 시절), 국내는 일봉 로테이션 봇과 계좌를 공유할 수 있어 이 봇이
    # 산 적도 없는 종목(로테이션 보유분)을 손절 청산하는 사고가 났다. 따라서 이
    # 봇은 **자기가 매수한 포지션(surge_trade)만** 관리하고 남의 보유분은 안 건드린다.

    def _reconcile_buys(self, held: dict, now: datetime) -> None:
        for sym, meta in list(self.pending_buys.items()):
            if sym in held and held[sym]["qty"] > 0:
                h = held[sym]
                # 주문때 만든 임시기록을 실제 체결가/수량으로 갱신
                self.store.update_trade_fill(meta["trade_id"], h["avg_price"], h["qty"])
                self.positions[sym] = {
                    "trade_id": meta["trade_id"], "entry_price": h["avg_price"],
                    "peak": h["avg_price"], "qty": h["qty"], "entry_ts": now,
                    "name": meta["name"]}
                log(f"  ✅ 진입체결: {meta['name']}({sym}) {h['qty']}주 @ {h['avg_price']:,.0f}원")
                del self.pending_buys[sym]
            elif (now - meta["ts"]).total_seconds() > 120:
                # 미체결 → 임시기록 삭제(주문이 안 잡힘)
                self.store.delete_trade(meta["trade_id"])
                log(f"  ⏳ 미체결 매수 취소: {meta['name']}({sym})")
                del self.pending_buys[sym]

    def _cleanup_vanished(self, held: dict, now: datetime) -> None:
        """추적 중인 포지션이 보유에도 없고 매도대기도 아니면 정리한다.
        (재시작 후 외부청산/미체결 잔재). 학습데이터 오염 방지 위해 행 삭제."""
        for sym, pos in list(self.positions.items()):
            if sym in held or sym in self.pending_sells:
                continue
            self.store.delete_trade(pos["trade_id"])
            log(f"  🧹 미보유 포지션 정리(외부청산/미체결): {pos['name']}({sym})")
            self.positions.pop(sym, None)

    def _reconcile_sells(self, held: dict, now: datetime) -> None:
        for sym, meta in list(self.pending_sells.items()):
            if sym not in held or held[sym]["qty"] == 0:
                pos = self.positions.get(sym)
                if pos:
                    exit_px = meta["exit_price"]
                    pnl = (exit_px - pos["entry_price"]) * pos["qty"]
                    pnl_pct = (exit_px / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0.0
                    hold_sec = int(self._hold_seconds(pos, now))
                    self.store.close_trade(
                        pos["trade_id"], now.strftime("%Y-%m-%d %H:%M:%S"),
                        exit_px, pnl, pnl_pct, meta["reason"], hold_sec)
                    log(f"  💰 청산: {pos['name']}({sym}) @ {exit_px:,.0f}원 "
                        f"손익 {pnl:+,.0f}원 ({pnl_pct:+.1f}%) [{meta['reason']}]")
                    self.positions.pop(sym, None)
                    self.recent_exits[sym] = now   # 재진입 쿨다운 시작
                self.pending_sells.pop(sym, None)

    def _hold_seconds(self, pos: dict, now: datetime) -> float:
        """보유 경과초. entry_ts 가 datetime(신규)이든 문자열(복구분)이든 처리."""
        ts = pos.get("entry_ts")
        if isinstance(ts, datetime):
            return (now - ts).total_seconds()
        try:
            return (now - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except (TypeError, ValueError):
            return 0.0

    def _check_exits(self, held: dict, now: datetime) -> None:
        for sym, pos in list(self.positions.items()):
            if sym in self.pending_sells or sym not in held:
                continue
            try:
                price = self.dom.current_price(sym, self.quote_market)
            except KISApiError:
                continue
            pos["peak"] = max(pos["peak"], price)
            pnl_pct = (price / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0.0
            drawdown = (price / pos["peak"] - 1) * 100 if pos["peak"] else 0.0

            reason = None
            if pnl_pct >= self.take_profit:
                reason = "익절"
            elif pnl_pct <= self.stop_loss:
                reason = "손절"
            elif pos["peak"] > pos["entry_price"] and drawdown <= self.trailing:
                reason = "트레일링"
            elif self.max_hold_sec > 0 and self._hold_seconds(pos, now) >= self.max_hold_sec:
                # 손익절에 안 걸리고 정체됨 → 강제 청산(자리 비워 리밸런싱)
                reason = "시간초과"
            if not reason:
                continue

            limit = limit_price("sell", price, self.sell_band_bps)
            if self.dry_run:
                log(f"  [dry] 📤 매도: {pos['name']}({sym}) @ {price:,}원 "
                    f"손익{pnl_pct:+.1f}% [{reason}]")
                continue
            try:
                self.dom.sell(sym, pos["qty"], limit)
                self.pending_sells[sym] = {"reason": reason, "exit_price": price}
                log(f"  📤 매도주문: {pos['name']}({sym}) @ {price:,}원→지정가{limit:,} [{reason}]")
            except KISApiError as e:
                log(f"  매도 오류 {sym}: {e}")

    def _enter_new(self, now: datetime) -> None:
        # 스캔·저장·호가수집은 '항상' 실행한다(보유가 꽉 차도 학습 데이터는 계속 쌓임).
        # 매수만 빈자리가 있을 때 한다. (스캔과 매수를 분리)
        scanned = self.scan()
        idx_chg = self._index_change()               # 시장 국면(지수 등락률)
        for r in scanned:
            r["index_chg"] = idx_chg                 # 학습 피처로 함께 저장
        if scanned:
            self.store.save_surge_scan(now.strftime("%Y-%m-%d %H:%M:%S"), scanned)
        self._capture_rebound_orderbook(scanned, now)  # 하락 반등 후보 호가 수집(데이터용)
        candidates = self.filter_candidates(scanned)
        self._capture_orderbook(candidates, now)     # 상위 후보 호가 임밸런스 수집·부착(학습 피처)
        self._rank_by_pressure(candidates)           # 매수세 우위 필터 + 거래대금/임밸런스 정렬
        self._apply_model(candidates, len(scanned))  # 모델 있으면 ML점수로 재정렬·필터(우선)

        room = self.max_positions - len(self.positions) - len(self.pending_buys)
        if self.dry_run:
            tag = " · ML점수순" if self._model_bundle and self.use_model else ""
            log(f"  스캔 {len(scanned)}종목 · 필터 통과 {len(candidates)}종목 · "
                f"빈자리 {room} · 지수 {idx_chg:+.2f}%{tag}")
        # 빈자리 없으면 매수만 건너뛴다(스캔·저장은 위에서 이미 완료 → 데이터는 계속 흐름).
        if room <= 0:
            return
        # 지수 국면 필터: 시장이 급락 중이면 신규 매수 중단(데이터 수집·청산은 계속)
        if self.regime_filter and idx_chg < self.regime_min_chg:
            log(f"  🚫 지수 국면 OFF (지수 {idx_chg:+.2f}% < {self.regime_min_chg}%) "
                f"— 신규 매수 중단")
            return
        for r in candidates[:room]:
            sym = r["symbol"]
            limit = limit_price("buy", r["price"], self.buy_band_bps)
            if self.order_value_krw > 0:
                qty = int(self.order_value_krw // limit) if limit > 0 else 0
            else:
                qty = self.order_qty
            if qty < 1:
                log(f"  매수 보류 {r['name']}({sym}): 예산부족(배정 {self.order_value_krw:,} < 1주 {limit:,})")
                continue
            # 진입 주문 타입: limit(지정가) / market(시장가) / best(최유리지정가).
            # 단타는 체결 보장이 중요 → market/best 로 '피 안 마르게' 진입 가능.
            if self.entry_order == "market":
                ord_dvsn, ord_price, ord_txt = "01", 0, "시장가"
            elif self.entry_order == "best":
                ord_dvsn, ord_price, ord_txt = "03", 0, "최유리"
            else:
                ord_dvsn, ord_price, ord_txt = "00", limit, f"지정가{limit:,}"
            if self.dry_run:
                log(f"  [dry] 📥 매수: {r['name']}({sym}) +{r['rate']:.1f}% "
                    f"@ {r['price']:,}원 x{qty} ({ord_txt})")
                continue
            try:
                self.dom.buy(sym, qty, ord_price, ord_dvsn=ord_dvsn)
                # 주문 즉시 기록(임시 진입가=현재 추정가). 체결되면 _reconcile_buys가 실제가로 갱신.
                # 이렇게 해야 --once/재시작에도 봇이 자기 포지션을 잃지 않는다.
                tid = self.store.open_trade({
                    "symbol": sym, "name": r["name"], "market": "",
                    "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": ord_price or r["price"], "qty": qty,
                    "entry_rate": r["rate"], "entry_volume": r["volume"],
                    "entry_rank": r.get("rank"), "entry_nscan": len(scanned),
                    "entry_ob_imbalance": r.get("ob_imbalance")})
                self.pending_buys[sym] = {
                    "trade_id": tid, "entry_rate": r["rate"], "volume": r["volume"],
                    "name": r["name"], "ts": now}
                log(f"  📥 매수주문: {r['name']}({sym}) +{r['rate']:.1f}% "
                    f"@ {r['price']:,}원 x{qty} → {ord_txt}")
            except KISApiError as e:
                log(f"  매수 오류 {sym}: {e}")

    # --- 실행 ---------------------------------------------------------------
    def run(self, once: bool = False) -> None:
        mode = "DRY-RUN(주문안함)" if self.dry_run else (
            "모의투자" if self.cfg.paper_trading else "!! 실전투자 !!")
        log(f"국내 급등주 봇 시작 [{mode}] | 등락률 {self.min_rate}~{self.max_rate}% "
            f"익절{self.take_profit}% 손절{self.stop_loss}% 트레일링{self.trailing}% "
            f"최대보유{self.max_positions}")
        if not kr_market_open():
            log("⚠️ 현재 국내 정규장(평일 09:00~15:30) 시간이 아닙니다. "
                "스캔은 직전 종가 기준이며 주문은 미체결될 수 있습니다.")
        if once or self.dry_run:
            self.tick()
            self._report()
            return  # --once / --dry-run 은 1주기만
        try:
            while True:
                self.tick()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log("사용자 중지.")
            self._report()
        finally:
            self.store.close()

    def _report(self) -> None:
        log(f"보유 {len(self.positions)} / 매수대기 {len(self.pending_buys)} "
            f"/ 매도대기 {len(self.pending_sells)}")
        for sym, p in self.positions.items():
            log(f"   - {p['name']}({sym}) {p['qty']}주 @ {p['entry_price']:,.0f}원")


def main() -> None:
    ap = argparse.ArgumentParser(description="국내 급등주 포착 자동매매(모의)")
    ap.add_argument("--once", action="store_true", help="1주기만 실행")
    ap.add_argument("--dry-run", action="store_true",
                    help="실제 주문 없이 스캔·신호 흐름만 점검(1주기)")
    args = ap.parse_args()
    SurgeBot(dry_run=args.dry_run).run(once=args.once)


if __name__ == "__main__":
    main()
