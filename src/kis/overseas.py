"""해외 주식(미국 등) 시세 조회 / 주문 / 잔고.

거래소 코드: NAS(나스닥) NYS(뉴욕) AMS(아멕스) / 주문용 ord 거래소: NASD NYSE AMEX
"""
from __future__ import annotations

from typing import Any

from .client import KISClient

# 시세조회용(EXCD) -> 주문용(OVRS_EXCG_CD) 매핑
_ORDER_EXCG = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}


class OverseasStock:
    def __init__(self, client: KISClient):
        self.c = client
        self.paper = client.config.paper_trading

    # --- 시세 ---------------------------------------------------------------
    def current_price(self, exchange: str, symbol: str) -> float:
        """현재가(달러) 조회. exchange: NAS/NYS/AMS, symbol: 예) AAPL."""
        data = self.c.get(
            "/uapi/overseas-price/v1/quotations/price",
            tr_id="HHDFS00000300",  # 시세는 모의/실전 동일
            params={"AUTH": "", "EXCD": exchange, "SYMB": symbol},
        )
        return float(data["output"]["last"])

    # --- 주문 ---------------------------------------------------------------
    def _order(self, exchange: str, symbol: str, qty: int, side: str, price: float) -> dict[str, Any]:
        # 미국 주식 모의/실전 TR_ID (매수 1002/매도 1001)
        if side == "buy":
            tr_id = "VTTT1002U" if self.paper else "TTTT1002U"
        elif side == "sell":
            tr_id = "VTTT1001U" if self.paper else "TTTT1006U"
        else:
            raise ValueError(f"알 수 없는 side: {side}")

        ovrs_excg = _ORDER_EXCG.get(exchange, exchange)
        body = {
            "CANO": self.c.config.account_no,
            "ACNT_PRDT_CD": self.c.config.account_product_code,
            "OVRS_EXCG_CD": ovrs_excg,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",  # 해외는 지정가 기본
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # 00=지정가
        }
        return self.c.post(
            "/uapi/overseas-stock/v1/trading/order", tr_id=tr_id, body=body
        )

    def buy(self, exchange: str, symbol: str, qty: int, price: float) -> dict[str, Any]:
        return self._order(exchange, symbol, qty, "buy", price)

    def sell(self, exchange: str, symbol: str, qty: int, price: float) -> dict[str, Any]:
        return self._order(exchange, symbol, qty, "sell", price)

    # --- 잔고 ---------------------------------------------------------------
    def balance(self) -> dict[str, Any]:
        tr_id = "VTTS3012R" if self.paper else "TTTS3012R"
        data = self.c.get(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": self.c.config.account_no,
                "ACNT_PRDT_CD": self.c.config.account_product_code,
                "OVRS_EXCG_CD": "NASD",
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        def _f(v, d=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d

        holdings = [
            {
                "symbol": row["ovrs_pdno"],
                "name": row["ovrs_item_name"],
                "qty": int(float(row["ovrs_cblc_qty"])),
                "avg_price": _f(row.get("pchs_avg_pric")),
                "cur_price": _f(row.get("now_pric2")),
                "buy_usd": _f(row.get("frcr_pchs_amt1")),
                "eval_usd": _f(row.get("ovrs_stck_evlu_amt")),
                "pnl": _f(row.get("frcr_evlu_pfls_amt")),
                "pnl_pct": _f(row.get("evlu_pfls_rt")),
            }
            for row in data.get("output1", [])
            if int(float(row.get("ovrs_cblc_qty", 0))) > 0
        ]
        return {"holdings": holdings}

    # --- 주문가능금액 / 환율 -----------------------------------------------
    def buy_power(self) -> dict[str, float]:
        """해외 주문가능 외화(USD)와 적용 환율(USD->KRW)."""
        tr_id = "VTTS3007R" if self.paper else "TTTS3007R"
        data = self.c.get(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            tr_id=tr_id,
            params={
                "CANO": self.c.config.account_no,
                "ACNT_PRDT_CD": self.c.config.account_product_code,
                "OVRS_EXCG_CD": "NASD",
                "OVRS_ORD_UNPR": "100",
                "ITEM_CD": "AAPL",
            },
        )
        o = data.get("output", {})
        return {
            "usd_orderable": float(o.get("ord_psbl_frcr_amt", 0) or 0),
            "fx_rate": float(o.get("exrt", 0) or 0),
        }

    def account_summary(self) -> dict[str, Any]:
        """USD 예수금/평가/손익 + 원화 환산 요약(+ 보유종목)."""
        bp = self.buy_power()
        holdings = self.balance()["holdings"]
        eval_usd = sum(h["eval_usd"] for h in holdings)
        pnl_usd = sum(h["pnl"] for h in holdings)
        total_usd = bp["usd_orderable"] + eval_usd
        fx = bp["fx_rate"]
        return {
            "usd_cash": bp["usd_orderable"],
            "fx_rate": fx,
            "eval_usd": eval_usd,
            "pnl_usd": pnl_usd,
            "total_usd": total_usd,
            "total_krw": total_usd * fx,
            "holdings": holdings,
        }

    def position_qty(self, symbol: str) -> int:
        for h in self.balance()["holdings"]:
            if h["symbol"] == symbol:
                return h["qty"]
        return 0

    # --- 급등주 스캔 --------------------------------------------------------
    def top_gainers(self, exchange: str, top: int = 30) -> list[dict[str, Any]]:
        """상승률 상위 종목. exchange: NAS/NYS/AMS.

        반환: [{exchange, symbol, name, price, rate(등락률%), volume}, ...]
        """
        data = self.c.get(
            "/uapi/overseas-stock/v1/ranking/updown-rate",
            tr_id="HHDFS76290000",
            params={
                "AUTH": "",
                "EXCD": exchange,
                "NDAY": "0",        # 당일
                "GUBN": "1",        # 1=상승률 상위, 0=하락률 상위
                "VOL_RANG": "0",    # 거래량 전체
                "KEYB": "",
            },
        )
        rows = []
        for r in data.get("output2", []):
            try:
                rows.append(
                    {
                        "exchange": exchange,
                        "symbol": r["symb"],
                        "name": r["name"],
                        "price": float(r["last"]),
                        "rate": float(r["rate"]),
                        "volume": int(r["tvol"]),
                    }
                )
            except (KeyError, ValueError):
                continue
        return rows[:top]
